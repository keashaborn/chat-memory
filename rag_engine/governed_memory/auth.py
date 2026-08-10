from __future__ import annotations

"""Verified-actor value objects; authentication itself is an outer adapter."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from .contracts import (
    ContractViolation,
    canonical_sha256,
    require_sha256,
    require_sorted_unique,
    require_utc,
    require_uuid,
)


class ActorRole(str, Enum):
    OWNER = "owner"
    WORKER = "worker"


class ActorScope(str, Enum):
    READ_CLAIMS = "read_claims"
    REVIEW_PROPOSALS = "review_proposals"
    MUTATE_CLAIMS = "mutate_claims"
    PROCESS_MEMORY_INGEST = "process_memory_ingest"
    LEASE_EXTRACTION = "lease_extraction"
    LEASE_PROJECTION = "lease_projection"
    FINALIZE_DELETION = "finalize_deletion"


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedActor:
    owner_user_id: UUID
    actor_id: UUID
    session_id: UUID
    role: ActorRole
    scopes: tuple[ActorScope, ...]
    authentication_manifest_sha256: str
    authenticated_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_actor_owner")
        require_uuid(self.actor_id, "invalid_actor_id")
        require_uuid(self.session_id, "invalid_actor_session")
        if not isinstance(self.role, ActorRole):
            raise ContractViolation("invalid_actor_role")
        if any(not isinstance(scope, ActorScope) for scope in self.scopes):
            raise ContractViolation("invalid_actor_scope")
        require_sorted_unique(
            self.scopes,
            code="actor_scopes_not_sorted_unique",
            key=lambda scope: scope.value,
        )
        require_sha256(
            self.authentication_manifest_sha256,
            "invalid_authentication_manifest_sha256",
        )
        require_utc(self.authenticated_at, "invalid_actor_timestamp")
        if self.role is ActorRole.OWNER and self.actor_id != self.owner_user_id:
            raise ContractViolation("owner_actor_mismatch")

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.verified_actor",
            {
                "owner_user_id": self.owner_user_id,
                "actor_id": self.actor_id,
                "session_id": self.session_id,
                "role": self.role,
                "scopes": self.scopes,
                "authentication_manifest_sha256": (
                    self.authentication_manifest_sha256
                ),
                "authenticated_at": self.authenticated_at,
            },
        )


def require_owner(actor: VerifiedActor, owner_user_id: UUID) -> None:
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_actor")
    require_uuid(owner_user_id, "invalid_expected_owner")
    if actor.owner_user_id != owner_user_id:
        raise ContractViolation("cross_owner_operation")


def require_scope(actor: VerifiedActor, scope: ActorScope) -> None:
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_actor")
    if not isinstance(scope, ActorScope) or scope not in actor.scopes:
        raise ContractViolation("actor_scope_denied")


__all__ = [
    "ActorRole",
    "ActorScope",
    "VerifiedActor",
    "require_owner",
    "require_scope",
]
