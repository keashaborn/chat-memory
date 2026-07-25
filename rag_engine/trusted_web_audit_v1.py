from __future__ import annotations

"""Postgres-backed rate limiting and metadata-only retrieval audit."""

import hashlib
import json
from uuid import UUID

from rag_engine.trusted_web_provider_v1 import TrustedWebSourceV1


def query_sha256(query: str) -> str:
    return hashlib.sha256(str(query).encode("utf-8")).hexdigest()


async def acquire_trusted_web_rate_limit_v1(
    conn,
    *,
    actor_user_id: UUID,
    requests_per_minute: int,
) -> bool:
    count = await conn.fetchval(
        """
        INSERT INTO trusted_web.request_rate_window (
            actor_user_id,
            window_start,
            request_count,
            updated_at
        )
        VALUES ($1, date_trunc('minute', now()), 1, now())
        ON CONFLICT (actor_user_id, window_start)
        DO UPDATE SET
            request_count = trusted_web.request_rate_window.request_count + 1,
            updated_at = now()
        RETURNING request_count
        """,
        actor_user_id,
    )
    return int(count) <= int(requests_per_minute)


async def start_trusted_web_audit_v1(
    conn,
    *,
    search_id: UUID,
    actor_user_id: UUID,
    request_id: str,
    query_hash: str,
    policy_version: str,
    topic: str,
    disposition: str,
    allowed_domains: tuple[str, ...],
) -> None:
    await conn.execute(
        """
        INSERT INTO trusted_web.retrieval_audit (
            search_id,
            actor_user_id,
            request_id,
            query_sha256,
            policy_version,
            topic,
            disposition,
            allowed_domains,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'started')
        """,
        search_id,
        actor_user_id,
        request_id,
        query_hash,
        policy_version,
        topic,
        disposition,
        list(allowed_domains),
    )


async def finish_trusted_web_audit_v1(
    conn,
    *,
    search_id: UUID,
    status: str,
    latency_ms: int,
    provider_response_id: str | None = None,
    sources: tuple[TrustedWebSourceV1, ...] = (),
    cited_sources: tuple[TrustedWebSourceV1, ...] = (),
    error_code: str | None = None,
) -> None:
    cited_urls = {source.url for source in cited_sources}
    source_metadata = [
        {
            **(
                source.model_dump(mode="json")
                if hasattr(source, "model_dump")
                else source.dict()
            ),
            "citation_status": (
                "cited" if source.url in cited_urls else "consulted"
            ),
        }
        for source in sources
    ]
    await conn.execute(
        """
        UPDATE trusted_web.retrieval_audit
        SET
            status = $2,
            provider_response_id = $3,
            source_metadata = $4::jsonb,
            source_count = $5,
            error_code = $6,
            latency_ms = $7,
            completed_at = now()
        WHERE search_id = $1
        """,
        search_id,
        status,
        provider_response_id,
        json.dumps(source_metadata, separators=(",", ":"), sort_keys=True),
        len(source_metadata),
        error_code,
        max(0, int(latency_ms)),
    )


__all__ = [
    "acquire_trusted_web_rate_limit_v1",
    "finish_trusted_web_audit_v1",
    "query_sha256",
    "start_trusted_web_audit_v1",
]
