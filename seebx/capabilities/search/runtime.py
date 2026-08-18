from __future__ import annotations

"""Shared runtime boundary for every SeeBx search policy profile."""

import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import Response

from seebx.capabilities.search.audit import (
    acquire_trusted_web_rate_limit_v1,
    finish_trusted_web_audit_v1,
    query_sha256,
    start_trusted_web_audit_v1,
)
from seebx.capabilities.search.provider import TrustedWebSourceV1


NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


class SearchRuntimeConfigurationError(RuntimeError):
    """Required server-owned search settings are absent or unsafe."""


class SearchAuditStoreUnavailableError(RuntimeError):
    """The canonical search audit store could not be opened."""


class SearchRateLimitExceededError(RuntimeError):
    """The actor exhausted the shared server-owned search rate limit."""


@dataclass(frozen=True)
class SearchRuntimeSettings:
    postgres_dsn: str
    safety_secret: str


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


def search_postgres_dsn_from_env() -> str:
    return (os.getenv("POSTGRES_DSN") or "").strip()


def search_runtime_settings_from_env() -> SearchRuntimeSettings:
    settings = SearchRuntimeSettings(
        postgres_dsn=search_postgres_dsn_from_env(),
        safety_secret=(os.getenv("VS_SERVICE_TOKEN") or "").strip(),
    )
    if not settings.postgres_dsn or len(settings.safety_secret) < 20:
        raise SearchRuntimeConfigurationError(
            "search_runtime_unconfigured"
        )
    return settings


def apply_search_no_store_headers(response: Response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        response.headers[name] = value


def safe_search_error_code(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text and len(text) <= 100 and all(
        char.isalnum() or char in {"_", "-"} for char in text
    ):
        return text
    return type(exc).__name__[:100]


__all__ = [
    "NO_STORE_HEADERS",
    "SearchAuditSession",
    "SearchAuditStoreUnavailableError",
    "SearchRateLimitExceededError",
    "SearchRuntimeConfigurationError",
    "SearchRuntimeSettings",
    "apply_search_no_store_headers",
    "open_search_audit_session",
    "safe_search_error_code",
    "search_postgres_dsn_from_env",
    "search_runtime_settings_from_env",
]
