"""Candidate-owned runtime adapters for the clean governed-Memory service.

HTTP dependencies are loaded only when an HTTP symbol is requested.  The
one-shot worker can therefore refuse disabled or malformed configuration
without importing FastAPI, JWT, cryptography, uvicorn, or opening any socket.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name in {
        "API_BIND_HOST",
        "API_BIND_PORT",
        "SUPABASE_API_KEY_ENV",
        "create_runtime_application",
    }:
        from . import application

        return getattr(application, name)
    if name in {
        "LiveSupabaseAuthorityConfig",
        "LiveSupabaseAuthorityVerifier",
        "LiveSupabaseSessionVerifier",
        "LiveSupabaseUserVerifier",
    }:
        from . import live_supabase

        return getattr(live_supabase, name)
    raise AttributeError(name)

__all__ = [
    "API_BIND_HOST",
    "API_BIND_PORT",
    "LiveSupabaseAuthorityConfig",
    "LiveSupabaseAuthorityVerifier",
    "LiveSupabaseSessionVerifier",
    "LiveSupabaseUserVerifier",
    "SUPABASE_API_KEY_ENV",
    "create_runtime_application",
]
