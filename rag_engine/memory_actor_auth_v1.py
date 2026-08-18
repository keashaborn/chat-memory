from __future__ import annotations

"""Temporary compatibility names for dormant pre-SeeBx capability modules."""

from seebx.core.identity import (
    ActorContext as MemoryActorContextV1,
    TEXT_AUTHORITY,
    VOICE_AUTHORITY,
    actor_authority as memory_actor_authority_v1,
    require_actor as require_memory_actor_v1,
    require_actor_context as require_memory_actor_context_v1,
)


__all__ = [
    "TEXT_AUTHORITY",
    "VOICE_AUTHORITY",
    "MemoryActorContextV1",
    "memory_actor_authority_v1",
    "require_memory_actor_context_v1",
    "require_memory_actor_v1",
]
