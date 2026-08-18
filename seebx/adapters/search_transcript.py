from __future__ import annotations

"""PostgreSQL connection boundary for persisted search transcripts."""

from typing import Any
from uuid import UUID

import asyncpg

from seebx.adapters.conversation_persistence import persist_search_exchange


class SearchTranscriptStoreUnavailableError(RuntimeError):
    pass


async def persist_search_exchange_with_dsn(
    *,
    postgres_dsn: str,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    query: str,
    answer: str,
    search_id: UUID,
    route: str,
    policy_version: str,
    decision: str,
    cited_sources: list[dict[str, Any]],
    admitted_sources: list[dict[str, Any]],
    consulted_source_count: int,
) -> UUID:
    """Connect, persist one atomic exchange, and always close the connection."""

    try:
        connection = await asyncpg.connect(
            postgres_dsn,
            command_timeout=15,
        )
    except Exception as exc:
        raise SearchTranscriptStoreUnavailableError(
            "search_transcript_store_unavailable"
        ) from exc

    try:
        return await persist_search_exchange(
            connection,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            request_id=request_id,
            query=query,
            answer=answer,
            search_id=search_id,
            route=route,
            policy_version=policy_version,
            decision=decision,
            cited_sources=cited_sources,
            admitted_sources=admitted_sources,
            consulted_source_count=consulted_source_count,
        )
    finally:
        await connection.close()


__all__ = [
    "SearchTranscriptStoreUnavailableError",
    "persist_search_exchange_with_dsn",
]
