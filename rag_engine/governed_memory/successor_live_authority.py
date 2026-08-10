from __future__ import annotations

"""Shared inert construction for successor live account/session authority."""

from collections.abc import Mapping
import os

from rag_engine.governed_memory.http_auth import HttpAuthError
from rag_engine.governed_memory.http_service import SUPABASE_ISSUER_ENV
from rag_engine.governed_memory.runtime.application import SUPABASE_API_KEY_ENV
from rag_engine.governed_memory.runtime.live_supabase import (
    LiveSupabaseAuthorityConfig,
    LiveSupabaseAuthorityVerifier,
    LiveSupabaseSessionVerifier,
    LiveSupabaseUserVerifier,
)


class SuccessorLiveAuthorityConfigurationError(RuntimeError):
    pass


def successor_live_authority_from_environment(
    environment: Mapping[str, str] | None = None,
) -> LiveSupabaseAuthorityVerifier:
    """Construct validated uncached user+auth.sessions checks without I/O."""

    values = os.environ if environment is None else environment
    try:
        config = LiveSupabaseAuthorityConfig(
            issuer=values.get(SUPABASE_ISSUER_ENV, ""),
            api_key=values.get(SUPABASE_API_KEY_ENV, ""),
        )
        return LiveSupabaseAuthorityVerifier(
            user_verifier=LiveSupabaseUserVerifier(config),
            session_verifier=LiveSupabaseSessionVerifier(config),
        )
    except HttpAuthError as exc:
        raise SuccessorLiveAuthorityConfigurationError(
            "successor_live_authority_unconfigured"
        ) from exc


__all__ = [
    "SuccessorLiveAuthorityConfigurationError",
    "successor_live_authority_from_environment",
]
