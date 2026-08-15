from __future__ import annotations

"""Owner-bound coordination between synchronous commands and async ingestion."""

from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import UUID


ChatMemoryIngestResolutionV1 = Literal["validate", "release", "suppress"]
_RESOLUTION_OUTCOMES = frozenset(
    {"absent", "released", "replayed", "suppressed", "validated"}
)


class ChatMemoryIngestCoordinationErrorV1(RuntimeError):
    """The transcript outbox could not be resolved safely."""


class ChatMemoryIngestCoordinationConnectionV1(Protocol):
    def transaction(self) -> Any: ...

    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchval(self, query: str, *args: object) -> object: ...


async def coordinate_chat_memory_ingest_v1(
    connection: ChatMemoryIngestCoordinationConnectionV1,
    *,
    owner_user_id: UUID,
    message_id: UUID | None,
    request_id: str | None,
    thread_id: UUID,
    message: str,
    resolution: ChatMemoryIngestResolutionV1,
) -> str:
    """Release ordinary chat ingestion or suppress a handled command."""

    if connection is None:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_connection_required"
        )
    if not isinstance(owner_user_id, UUID):
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_owner_invalid"
        )
    if not isinstance(thread_id, UUID):
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_source_invalid"
        )
    if message_id is not None and not isinstance(message_id, UUID):
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_source_invalid"
        )
    if request_id is not None and (
        not isinstance(request_id, str)
        or request_id != request_id.strip()
        or not 1 <= len(request_id) <= 128
    ):
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_source_invalid"
        )
    if message_id is None and request_id is None:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_source_invalid"
        )
    if not isinstance(message, str) or not message:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_message_invalid"
        )
    if resolution not in {"validate", "release", "suppress"}:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_resolution_invalid"
        )
    content_sha256 = sha256(message.encode("utf-8")).hexdigest()
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner_user_id),
            )
            outcome = await connection.fetchval(
                "SELECT memory_ingest_private."
                "resolve_chat_memory_command("
                "$1::uuid,$2::text,$3::uuid,$4::text,$5::text)",
                message_id,
                request_id,
                thread_id,
                content_sha256,
                resolution,
            )
    except Exception as exc:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_unavailable"
        ) from exc
    if outcome not in _RESOLUTION_OUTCOMES:
        raise ChatMemoryIngestCoordinationErrorV1(
            "memory_ingest_coordination_receipt_invalid"
        )
    return str(outcome)


__all__ = [
    "ChatMemoryIngestCoordinationConnectionV1",
    "ChatMemoryIngestCoordinationErrorV1",
    "ChatMemoryIngestResolutionV1",
    "coordinate_chat_memory_ingest_v1",
]
