"""Authority, policy, configuration, audit, and error boundaries."""

from .identity import (
    ActorContext,
    TEXT_AUTHORITY,
    VOICE_AUTHORITY,
    actor_authority,
    require_actor,
    require_actor_context,
)

__all__ = [
    "ActorContext",
    "TEXT_AUTHORITY",
    "VOICE_AUTHORITY",
    "actor_authority",
    "require_actor",
    "require_actor_context",
]
