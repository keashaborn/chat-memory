from __future__ import annotations

"""Shared runtime boundary for every SeeBx search policy profile."""

import os
from dataclasses import dataclass

from fastapi import Response


NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


class SearchRuntimeConfigurationError(RuntimeError):
    """Required server-owned search settings are absent or unsafe."""


@dataclass(frozen=True)
class SearchRuntimeSettings:
    postgres_dsn: str
    safety_secret: str


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
    "SearchRuntimeConfigurationError",
    "SearchRuntimeSettings",
    "apply_search_no_store_headers",
    "safe_search_error_code",
    "search_postgres_dsn_from_env",
    "search_runtime_settings_from_env",
]
