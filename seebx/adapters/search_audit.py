from __future__ import annotations

"""PostgreSQL rate-limit and metadata-only search audit adapter."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from seebx.capabilities.search.audit import query_sha256
from seebx.capabilities.search.provider import TrustedWebSourceV1
from seebx.capabilities.search.runtime import (
    SearchAuditStoreUnavailableError,
    SearchRateLimitExceededError,
)


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
    admitted_sources: tuple[TrustedWebSourceV1, ...] = (),
    rejected_source_reasons: tuple[tuple[str, str], ...] = (),
    error_code: str | None = None,
) -> None:
    cited_urls = {source.url for source in cited_sources}
    admitted_urls = {source.url for source in admitted_sources}
    rejected_by_url = dict(rejected_source_reasons)
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
            "admission_status": (
                "admitted"
                if source.url in admitted_urls
                else "provider_observed_only"
            ),
            "admission_reason": (
                "cited"
                if source.url in cited_urls
                else (
                    "policy_relevant"
                    if source.url in admitted_urls
                    else rejected_by_url.get(source.url, "not_admitted")
                )
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


@dataclass
class SearchAuditSession:
    """One started search audit and its exclusively owned connection."""

    connection: Any
    search_id: UUID
    _closed: bool = False

    async def finish(
        self,
        *,
        status: str,
        latency_ms: int,
        provider_response_id: str | None = None,
        sources: tuple[TrustedWebSourceV1, ...] = (),
        cited_sources: tuple[TrustedWebSourceV1, ...] = (),
        admitted_sources: tuple[TrustedWebSourceV1, ...] = (),
        rejected_source_reasons: tuple[tuple[str, str], ...] = (),
        error_code: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("search_audit_session_closed")
        await finish_trusted_web_audit_v1(
            self.connection,
            search_id=self.search_id,
            status=status,
            latency_ms=latency_ms,
            provider_response_id=provider_response_id,
            sources=sources,
            cited_sources=cited_sources,
            admitted_sources=admitted_sources,
            rejected_source_reasons=rejected_source_reasons,
            error_code=error_code,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.connection.close()


async def open_search_audit_session(
    *,
    postgres_dsn: str,
    actor_user_id: UUID,
    requests_per_minute: int,
    search_id: UUID,
    request_id: str,
    query: str,
    policy_version: str,
    topic: str,
    disposition: str,
    allowed_domains: tuple[str, ...],
) -> SearchAuditSession:
    """Open, rate-limit, and start one audit or leave no connection open."""

    try:
        connection = await asyncpg.connect(
            postgres_dsn,
            command_timeout=15,
        )
    except Exception as exc:
        raise SearchAuditStoreUnavailableError(
            "search_audit_store_unavailable"
        ) from exc

    session = SearchAuditSession(
        connection=connection,
        search_id=search_id,
    )
    try:
        allowed = await acquire_trusted_web_rate_limit_v1(
            connection,
            actor_user_id=actor_user_id,
            requests_per_minute=requests_per_minute,
        )
        if not allowed:
            raise SearchRateLimitExceededError("search_rate_limited")
        await start_trusted_web_audit_v1(
            connection,
            search_id=search_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            query_hash=query_sha256(query),
            policy_version=policy_version,
            topic=topic,
            disposition=disposition,
            allowed_domains=allowed_domains,
        )
    except Exception:
        await session.close()
        raise
    return session




__all__ = [
    "SearchAuditSession",
    "acquire_trusted_web_rate_limit_v1",
    "finish_trusted_web_audit_v1",
    "open_search_audit_session",
    "start_trusted_web_audit_v1",
]
