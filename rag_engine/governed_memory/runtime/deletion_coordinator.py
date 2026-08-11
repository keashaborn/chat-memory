from __future__ import annotations

"""Inactive, bounded coordinator for the two-database deletion saga.

One invocation leases at most one operation and prepares at most one claim
deletion.  The final chat-deletion/ack boundary is replayable after a crash.
It is wired only into the mode-off candidate one-shot worker; no HTTP route,
service, timer, or production membership activates it.
"""

from ..contracts import ContractViolation
from ..conversation_deletion import (
    ConversationDeletionRepository,
    DeletionRepositoryError,
    DeletionRepositoryFailure,
    SuccessorDeletionRepository,
    require_claim_step_binding,
    require_finalization_receipt_binding,
    require_mutation_receipt_binding,
    require_progress_binding,
    require_source_receipt_binding,
    validate_erasure_targets,
)
from ..deletion_contracts import (
    ConversationErasureLease,
    ConversationErasureState,
    DeletionCoordinatorOutcome,
    DeletionCoordinatorReceipt,
    SuccessorErasureProgress,
    SuccessorErasureState,
    deletion_coordinator_receipt_sha256,
)


DELETION_COORDINATOR_WORKER_ID = "governed-memory-deletion-coordinator-1"
DEFAULT_DELETION_LEASE_SECONDS = 120


class InactiveDeletionCoordinator:
    """Replay-safe composition inside the still-disabled candidate worker."""

    def __init__(
        self,
        *,
        conversation: ConversationDeletionRepository,
        successor: SuccessorDeletionRepository,
        worker_id: str = DELETION_COORDINATOR_WORKER_ID,
        lease_seconds: int = DEFAULT_DELETION_LEASE_SECONDS,
    ) -> None:
        if conversation is None:
            raise ContractViolation("conversation_deletion_repository_required")
        if successor is None:
            raise ContractViolation("successor_deletion_repository_required")
        if worker_id != DELETION_COORDINATOR_WORKER_ID:
            raise ContractViolation("deletion_coordinator_worker_id_mismatch")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ContractViolation("invalid_deletion_coordinator_lease")
        self._conversation = conversation
        self._successor = successor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    @staticmethod
    def _receipt(
        lease: ConversationErasureLease,
        *,
        outcome: DeletionCoordinatorOutcome,
        conversation_state: ConversationErasureState,
        successor_state: SuccessorErasureState,
        affected_claim_count: int,
        pending_claim_deletions: int,
    ) -> DeletionCoordinatorReceipt:
        material = {
            "owner_user_id": lease.owner_user_id,
            "operation_id": lease.operation_id,
            "outcome": outcome,
            "conversation_state": conversation_state,
            "successor_state": successor_state,
            "target_count": lease.target_count,
            "affected_claim_count": affected_claim_count,
            "pending_claim_deletions": pending_claim_deletions,
            "binding_sha256": lease.work_binding_sha256,
            "selector_sha256": lease.selector_sha256,
            "target_manifest_sha256": lease.target_manifest_sha256,
        }
        return DeletionCoordinatorReceipt(
            **material,
            receipt_sha256=deletion_coordinator_receipt_sha256(**material),
        )

    async def _finish_conversation_and_ack(
        self,
        lease: ConversationErasureLease,
        *,
        affected_claim_count: int,
    ) -> DeletionCoordinatorReceipt:
        conversation_receipt = require_finalization_receipt_binding(
            lease,
            await self._conversation.finalize_conversation_erasure(lease),
        )
        require_mutation_receipt_binding(
            lease,
            await self._successor.acknowledge_conversation_deleted(
                lease,
                conversation_receipt_sha256=(
                    conversation_receipt.receipt_sha256
                ),
            ),
        )
        final_receipt = await self._successor.read_source_erasure_receipt(lease)
        if final_receipt is None:
            raise ContractViolation("source_erasure_receipt_missing_after_ack")
        checked_final = require_source_receipt_binding(lease, final_receipt)
        if (
            checked_final.conversation_receipt_sha256
            != conversation_receipt.receipt_sha256
        ):
            raise ContractViolation("conversation_erasure_receipt_ack_mismatch")
        await self._conversation.acknowledge_erasure_completion(
            lease,
            conversation_receipt_sha256=conversation_receipt.receipt_sha256,
        )
        return self._receipt(
            lease,
            outcome=DeletionCoordinatorOutcome.COMPLETED,
            conversation_state=ConversationErasureState.COMPLETED,
            successor_state=SuccessorErasureState.COMPLETED,
            affected_claim_count=checked_final.touched_claim_count,
            pending_claim_deletions=0,
        )

    async def _progress(
        self,
        lease: ConversationErasureLease,
    ) -> SuccessorErasureProgress:
        return require_progress_binding(
            lease,
            await self._successor.read_source_erasure_progress(lease),
        )

    async def _record_receipt_invalid(
        self,
        lease: ConversationErasureLease,
        *,
        preserve_pending_ack: bool,
        successor_state: SuccessorErasureState,
        affected_claim_count: int,
        pending_claim_deletions: int,
    ) -> DeletionCoordinatorReceipt:
        """Persist a contract failure without claiming deleted chat returned."""

        failed_state = await self._conversation.fail_erasure(
            lease,
            failure=DeletionRepositoryFailure.RECEIPT_INVALID,
            retry_after_seconds=30 if preserve_pending_ack else 0,
        )
        if failed_state is ConversationErasureState.MANUAL_REVIEW:
            outcome = DeletionCoordinatorOutcome.MANUAL_REVIEW
        elif failed_state is (
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
        ):
            outcome = DeletionCoordinatorOutcome.RETRYABLE
        else:
            raise ContractViolation(
                "receipt_invalid_failure_disposition_mismatch"
            )
        return self._receipt(
            lease,
            outcome=outcome,
            conversation_state=failed_state,
            successor_state=successor_state,
            affected_claim_count=affected_claim_count,
            pending_claim_deletions=pending_claim_deletions,
        )

    async def advance_one(self) -> DeletionCoordinatorReceipt | None:
        """Advance one DB-leased operation; ``None`` means no work."""

        lease = await self._conversation.lease_erasure(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return None
        if not isinstance(lease, ConversationErasureLease):
            raise ContractViolation("invalid_conversation_erasure_lease")

        preserve_pending_ack = lease.state is (
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
        )
        successor_state = (
            SuccessorErasureState.MEMORY_DELETED
            if lease.state
            in {
                ConversationErasureState.GOVERNED_DELETED,
                ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            }
            else SuccessorErasureState.RECEIVING
        )
        affected_claim_count = 0
        pending_claim_deletions = 0
        try:
            if lease.state is (
                ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
            ):
                # Targets were already erased.  The stored conversation receipt
                # makes finalization replayable without reconstructing them.
                return await self._finish_conversation_and_ack(
                    lease,
                    affected_claim_count=0,
                )

            targets = await self._conversation.read_erasure_targets(lease)
            validate_erasure_targets(lease, targets)

            if lease.state is ConversationErasureState.GOVERNED_DELETED:
                # Once finalization begins, a malformed returned receipt may
                # follow a committed physical chat deletion.  Preserve the
                # pending-ack state conservatively on every failure thereafter.
                preserve_pending_ack = True
                return await self._finish_conversation_and_ack(
                    lease,
                    affected_claim_count=0,
                )

            require_mutation_receipt_binding(
                lease,
                await self._successor.stage_source_erasure(lease),
            )
            progress = await self._progress(lease)
            successor_state = progress.state
            affected_claim_count = progress.affected_claim_count
            pending_claim_deletions = progress.pending_claim_deletions
            if progress.state is SuccessorErasureState.RECEIVING:
                require_mutation_receipt_binding(
                    lease,
                    await self._successor.stage_source_erasure_targets(
                        lease,
                        targets,
                    ),
                )
                require_mutation_receipt_binding(
                    lease,
                    await self._successor.seal_source_erasure_targets(lease),
                )
                progress = await self._progress(lease)
                successor_state = progress.state
                affected_claim_count = progress.affected_claim_count
                pending_claim_deletions = progress.pending_claim_deletions

            if progress.state is SuccessorErasureState.MANUAL_REVIEW:
                return await self._record_receipt_invalid(
                    lease,
                    successor_state=SuccessorErasureState.MANUAL_REVIEW,
                    preserve_pending_ack=False,
                    affected_claim_count=progress.affected_claim_count,
                    pending_claim_deletions=(
                        progress.pending_claim_deletions
                    ),
                )

            if progress.state in {
                SuccessorErasureState.FENCED,
                SuccessorErasureState.CLAIM_DELETION_PENDING,
            }:
                require_claim_step_binding(
                    lease,
                    await self._successor.request_next_source_erasure_claim_deletion(
                        lease
                    ),
                )
                progress = await self._progress(lease)
                successor_state = progress.state
                affected_claim_count = progress.affected_claim_count
                pending_claim_deletions = progress.pending_claim_deletions

            if progress.pending_claim_deletions != 0:
                await self._conversation.release_erasure_lease(lease)
                return self._receipt(
                    lease,
                    outcome=DeletionCoordinatorOutcome.PROCESSING,
                    conversation_state=(
                        ConversationErasureState.GOVERNED_DELETION_PENDING
                    ),
                    successor_state=progress.state,
                    affected_claim_count=progress.affected_claim_count,
                    pending_claim_deletions=(
                        progress.pending_claim_deletions
                    ),
                )

            if progress.state is SuccessorErasureState.MEMORY_DELETED:
                governed_receipt_sha256 = progress.governed_receipt_sha256
                if governed_receipt_sha256 is None:
                    raise ContractViolation(
                        "missing_successor_governed_receipt"
                    )
            else:
                governed = require_mutation_receipt_binding(
                    lease,
                    await self._successor.finalize_source_erasure_memory(lease),
                )
                governed_receipt_sha256 = governed.material_sha256
                successor_state = SuccessorErasureState.MEMORY_DELETED
                pending_claim_deletions = 0

            # This durable transition clears the current conversation lease.
            # A later invocation re-leases `governed_deleted` before physical
            # chat deletion, avoiding a false cross-database transaction.
            await self._conversation.mark_successor_memory_deleted(
                lease,
                governed_receipt_sha256=governed_receipt_sha256,
            )
            return self._receipt(
                lease,
                outcome=DeletionCoordinatorOutcome.PROCESSING,
                conversation_state=ConversationErasureState.GOVERNED_DELETED,
                successor_state=SuccessorErasureState.MEMORY_DELETED,
                affected_claim_count=progress.affected_claim_count,
                pending_claim_deletions=0,
            )
        except DeletionRepositoryError as exc:
            failed_state = await self._conversation.fail_erasure(
                lease,
                failure=exc.failure,
                retry_after_seconds=30,
            )
            return self._receipt(
                lease,
                outcome=DeletionCoordinatorOutcome.RETRYABLE,
                conversation_state=failed_state,
                successor_state=successor_state,
                affected_claim_count=affected_claim_count,
                pending_claim_deletions=pending_claim_deletions,
            )
        except ContractViolation:
            return await self._record_receipt_invalid(
                lease,
                preserve_pending_ack=preserve_pending_ack,
                successor_state=successor_state,
                affected_claim_count=affected_claim_count,
                pending_claim_deletions=pending_claim_deletions,
            )


__all__ = [
    "DEFAULT_DELETION_LEASE_SECONDS",
    "DELETION_COORDINATOR_WORKER_ID",
    "InactiveDeletionCoordinator",
]
