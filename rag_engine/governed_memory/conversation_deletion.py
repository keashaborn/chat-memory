from __future__ import annotations

"""Closed repository boundary for the two-PostgreSQL deletion saga.

The request path is owner-authenticated.  The coordinator path trusts only a
lease returned by the conversation database; it never accepts an owner from an
HTTP body or coordinator caller.
"""

from enum import Enum
from typing import Protocol

from .contracts import ContractViolation, require_exact_int, require_sha256
from .deletion_contracts import (
    BoundConversationDeletion,
    ClaimDeletionStepReceipt,
    ConversationErasureLease,
    ConversationErasureState,
    ConversationErasureStatus,
    ConversationErasureTarget,
    ConversationFinalizationReceipt,
    DeletionMutationReceipt,
    SourceErasureReceipt,
    SuccessorErasureProgress,
    erasure_target_manifest_sha256,
)


class DeletionRepositoryFailure(str, Enum):
    CONVERSATION_UNAVAILABLE = "conversation_unavailable"
    SUCCESSOR_UNAVAILABLE = "successor_unavailable"
    LEASE_LOST = "lease_lost"
    RECEIPT_INVALID = "receipt_invalid"
    LEGACY_PROJECT_THREAD_DEPENDENCY = (
        "legacy_project_memory_thread_dependencies_not_separated"
    )


class DeletionRepositoryError(RuntimeError):
    def __init__(self, failure: DeletionRepositoryFailure) -> None:
        if not isinstance(failure, DeletionRepositoryFailure):
            failure = DeletionRepositoryFailure.RECEIPT_INVALID
        self.failure = failure
        if failure is DeletionRepositoryFailure.LEGACY_PROJECT_THREAD_DEPENDENCY:
            self.code = "governed_project_thread_erasure_required"
            self.status_code = 409
            self.retryable = False
        elif failure is DeletionRepositoryFailure.RECEIPT_INVALID:
            self.code = failure.value
            self.status_code = 500
            self.retryable = False
        else:
            self.code = failure.value
            self.status_code = 503
            self.retryable = True
        super().__init__(failure.value)


class ConversationDeletionRepository(Protocol):
    async def request_erasure(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus: ...

    async def read_erasure_status(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus | None: ...

    async def lease_erasure(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> ConversationErasureLease | None: ...

    async def read_erasure_targets(
        self,
        lease: ConversationErasureLease,
    ) -> tuple[ConversationErasureTarget, ...]: ...

    async def release_erasure_lease(
        self,
        lease: ConversationErasureLease,
    ) -> None: ...

    async def mark_successor_memory_deleted(
        self,
        lease: ConversationErasureLease,
        *,
        governed_receipt_sha256: str,
    ) -> None: ...

    async def finalize_conversation_erasure(
        self,
        lease: ConversationErasureLease,
    ) -> ConversationFinalizationReceipt: ...

    async def acknowledge_erasure_completion(
        self,
        lease: ConversationErasureLease,
        *,
        conversation_receipt_sha256: str,
    ) -> None: ...

    async def fail_erasure(
        self,
        lease: ConversationErasureLease,
        *,
        failure: DeletionRepositoryFailure,
        retry_after_seconds: int,
    ) -> ConversationErasureState: ...


class SuccessorDeletionRepository(Protocol):
    async def stage_source_erasure(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt: ...

    async def stage_source_erasure_targets(
        self,
        lease: ConversationErasureLease,
        targets: tuple[ConversationErasureTarget, ...],
    ) -> DeletionMutationReceipt: ...

    async def seal_source_erasure_targets(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt: ...

    async def request_next_source_erasure_claim_deletion(
        self,
        lease: ConversationErasureLease,
    ) -> ClaimDeletionStepReceipt: ...

    async def read_source_erasure_progress(
        self,
        lease: ConversationErasureLease,
    ) -> SuccessorErasureProgress: ...

    async def finalize_source_erasure_memory(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt: ...

    async def acknowledge_conversation_deleted(
        self,
        lease: ConversationErasureLease,
        *,
        conversation_receipt_sha256: str,
    ) -> DeletionMutationReceipt: ...

    async def read_source_erasure_receipt(
        self,
        lease: ConversationErasureLease,
    ) -> SourceErasureReceipt | None: ...


def require_request_status_binding(
    command: BoundConversationDeletion,
    status: ConversationErasureStatus,
) -> ConversationErasureStatus:
    if not isinstance(command, BoundConversationDeletion):
        raise ContractViolation("invalid_deletion_command")
    if not isinstance(status, ConversationErasureStatus) or (
        status.owner_user_id != command.authority.owner_user_id
        or status.operation_id != command.operation_id
        or status.selector_kind is not command.request.selector_kind
    ):
        raise ContractViolation("conversation_erasure_status_binding_mismatch")
    return status


def require_mutation_receipt_binding(
    lease: ConversationErasureLease,
    receipt: DeletionMutationReceipt,
) -> DeletionMutationReceipt:
    if not isinstance(lease, ConversationErasureLease):
        raise ContractViolation("invalid_conversation_erasure_lease")
    if not isinstance(receipt, DeletionMutationReceipt) or (
        receipt.owner_user_id != lease.owner_user_id
        or receipt.operation_id != lease.operation_id
    ):
        raise ContractViolation("deletion_mutation_receipt_binding_mismatch")
    return receipt


def require_claim_step_binding(
    lease: ConversationErasureLease,
    receipt: ClaimDeletionStepReceipt,
) -> ClaimDeletionStepReceipt:
    if not isinstance(receipt, ClaimDeletionStepReceipt) or (
        receipt.owner_user_id != lease.owner_user_id
        or receipt.operation_id != lease.operation_id
    ):
        raise ContractViolation("claim_deletion_step_binding_mismatch")
    return receipt


def require_progress_binding(
    lease: ConversationErasureLease,
    progress: SuccessorErasureProgress,
) -> SuccessorErasureProgress:
    if not isinstance(progress, SuccessorErasureProgress) or (
        progress.owner_user_id != lease.owner_user_id
        or progress.operation_id != lease.operation_id
        or progress.target_count != lease.target_count
        or progress.target_manifest_sha256
        != lease.target_manifest_sha256
    ):
        raise ContractViolation("successor_erasure_progress_binding_mismatch")
    return progress


def require_source_receipt_binding(
    lease: ConversationErasureLease,
    receipt: SourceErasureReceipt,
) -> SourceErasureReceipt:
    if not isinstance(receipt, SourceErasureReceipt) or (
        receipt.operation_id != lease.operation_id
        or receipt.selector_sha256 != lease.selector_sha256
        or receipt.target_count != lease.target_count
        or receipt.target_manifest_sha256
        != lease.target_manifest_sha256
    ):
        raise ContractViolation("source_erasure_receipt_binding_mismatch")
    return receipt


def validate_erasure_targets(
    lease: ConversationErasureLease,
    targets: tuple[ConversationErasureTarget, ...],
) -> str:
    """Verify exact DB target rows and the sealed target manifest.

    SQL is the selector authority.  Python does not reinterpret a timestamp
    boundary; it verifies each target hash, ordering, count, owner/operation
    binding, and the complete target manifest.
    """

    if not isinstance(lease, ConversationErasureLease):
        raise ContractViolation("invalid_conversation_erasure_lease")
    require_exact_int(
        len(targets),
        code="invalid_erasure_target_count",
        maximum=100_000,
    )
    if len(targets) != lease.target_count:
        raise ContractViolation("erasure_target_count_mismatch")
    manifest = erasure_target_manifest_sha256(
        owner_user_id=lease.owner_user_id,
        operation_id=lease.operation_id,
        targets=targets,
    )
    if manifest != lease.target_manifest_sha256:
        raise ContractViolation("erasure_target_manifest_mismatch")
    return manifest
def require_finalization_receipt_binding(
    lease: ConversationErasureLease,
    receipt: ConversationFinalizationReceipt,
) -> ConversationFinalizationReceipt:
    if not isinstance(receipt, ConversationFinalizationReceipt) or (
        receipt.owner_user_id != lease.owner_user_id
        or receipt.operation_id != lease.operation_id
        or receipt.deleted_message_count > lease.target_count
        or receipt.deleted_bridge_row_count > lease.target_count
    ):
        raise ContractViolation("conversation_finalization_receipt_mismatch")
    require_sha256(
        receipt.receipt_sha256,
        "invalid_conversation_finalization_receipt",
    )
    return receipt


__all__ = [
    "ConversationDeletionRepository",
    "DeletionRepositoryError",
    "DeletionRepositoryFailure",
    "SuccessorDeletionRepository",
    "require_claim_step_binding",
    "require_finalization_receipt_binding",
    "require_mutation_receipt_binding",
    "require_progress_binding",
    "require_request_status_binding",
    "require_source_receipt_binding",
    "validate_erasure_targets",
]
