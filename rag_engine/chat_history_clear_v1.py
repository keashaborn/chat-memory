from __future__ import annotations

"""Owner-scoped visible chat-history clearing that preserves Memory."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID


CHAT_HISTORY_CLEAR_CONTRACT_VERSION = "chat_history_clear_v1"
_BEARER_RE = re.compile(
    r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
    re.ASCII | re.IGNORECASE,
)
_RESULT_FIELDS = (
    "outcome",
    "operation_id",
    "scope",
    "deleted_message_count",
    "deleted_thread_count",
    "deleted_outbox_count",
    "receipt_sha256",
    "completed_at",
)


class ChatHistoryClearError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatHistoryClearResultV1:
    operation_id: UUID
    scope: Literal["all", "recent", "thread", "message_tail"]
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


def authorization_manifest_sha256(authorization: str) -> str:
    if not isinstance(authorization, str) or len(authorization) > 16_384:
        raise ChatHistoryClearError("unauthorized", status_code=401)
    if _BEARER_RE.fullmatch(authorization) is None:
        raise ChatHistoryClearError("unauthorized", status_code=401)
    return hashlib.sha256(authorization.encode("ascii")).hexdigest()


def _canonical_uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ChatHistoryClearError(code, status_code=503) from exc
    if str(parsed) != str(value):
        raise ChatHistoryClearError(code, status_code=503)
    return parsed


def _bounded_count(value: object, code: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ChatHistoryClearError(code, status_code=503)
    return value


async def clear_chat_history_v1(
    connection: Any,
    *,
    owner_user_id: UUID,
    authorization: str,
    operation_id: UUID,
    scope: Literal["all", "recent", "thread", "message_tail"],
    thread_id: UUID | None = None,
    recent_window_seconds: int | None = None,
) -> ChatHistoryClearResultV1:
    if connection is None:
        raise ChatHistoryClearError("chat_history_unavailable", status_code=503)
    if not isinstance(owner_user_id, UUID) or not isinstance(operation_id, UUID):
        raise ChatHistoryClearError("invalid_chat_history_request", status_code=400)
    if scope not in {"all", "recent", "thread", "message_tail"}:
        raise ChatHistoryClearError("invalid_chat_history_request", status_code=400)
    if scope in {"thread", "message_tail"}:
        if not isinstance(thread_id, UUID) or recent_window_seconds is not None:
            raise ChatHistoryClearError("invalid_chat_history_request", status_code=400)
    elif scope == "recent":
        if thread_id is not None or recent_window_seconds not in {
            3_600,
            86_400,
            604_800,
            2_592_000,
        }:
            raise ChatHistoryClearError("invalid_chat_history_request", status_code=400)
    elif thread_id is not None or recent_window_seconds is not None:
        raise ChatHistoryClearError("invalid_chat_history_request", status_code=400)

    auth_sha256 = authorization_manifest_sha256(authorization)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.set_config('app.user_id',$1::text,true)",
                str(owner_user_id),
            )
            await connection.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256',$1::text,true)",
                auth_sha256,
            )
            if scope == "message_tail":
                value = await connection.fetchrow(
                    "SELECT * FROM chat_history_private.clear_message_tail("
                    "$1::uuid,$2::uuid)",
                    operation_id,
                    thread_id,
                )
            else:
                value = await connection.fetchrow(
                    "SELECT * FROM chat_history_private.clear_history("
                    "$1::uuid,$2::text,$3::uuid,$4::integer)",
                    operation_id,
                    scope,
                    thread_id,
                    recent_window_seconds,
                )
    except ChatHistoryClearError:
        raise
    except Exception as error:
        message = str(getattr(error, "message", ""))
        if message == "chat memory is still processing":
            raise ChatHistoryClearError(
                "chat_memory_still_processing",
                status_code=409,
            ) from None
        if message == "chat history thread is absent":
            raise ChatHistoryClearError("thread_not_found", status_code=404) from None
        if message == "chat history message is absent":
            raise ChatHistoryClearError(
                "message_not_found", status_code=404
            ) from None
        if message == "invalid chat history clear request":
            raise ChatHistoryClearError(
                "invalid_chat_history_request",
                status_code=400,
            ) from None
        raise ChatHistoryClearError("chat_history_unavailable", status_code=503) from error

    row = dict(value) if value is not None else {}
    if tuple(row) != _RESULT_FIELDS or row.get("outcome") not in {
        "cleared",
        "replayed",
    }:
        raise ChatHistoryClearError("chat_history_unavailable", status_code=503)
    result_scope = row["scope"]
    if result_scope != scope:
        raise ChatHistoryClearError("chat_history_unavailable", status_code=503)
    receipt_sha256 = row["receipt_sha256"]
    if (
        not isinstance(receipt_sha256, str)
        or len(receipt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in receipt_sha256)
    ):
        raise ChatHistoryClearError("chat_history_unavailable", status_code=503)
    return ChatHistoryClearResultV1(
        operation_id=_canonical_uuid(row["operation_id"], "chat_history_unavailable"),
        scope=result_scope,
        deleted_message_count=_bounded_count(
            row["deleted_message_count"], "chat_history_unavailable"
        ),
        deleted_thread_count=_bounded_count(
            row["deleted_thread_count"], "chat_history_unavailable"
        ),
        deleted_outbox_count=_bounded_count(
            row["deleted_outbox_count"], "chat_history_unavailable"
        ),
        receipt_sha256=receipt_sha256,
        completed_at=row["completed_at"],
    )


__all__ = [
    "CHAT_HISTORY_CLEAR_CONTRACT_VERSION",
    "ChatHistoryClearError",
    "ChatHistoryClearResultV1",
    "authorization_manifest_sha256",
    "clear_chat_history_v1",
]
