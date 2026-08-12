from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
)
from rag_engine.governed_memory.deletion_contracts import (
    ClaimDeletionStepOutcome,
    ClaimDeletionStepReceipt,
    ConversationErasureLease,
    ConversationErasureState,
    ConversationErasureTarget,
    ConversationFinalizationReceipt,
    DeletionCoordinatorOutcome,
    DeletionMutationOutcome,
    DeletionMutationReceipt,
    DeletionSelectorKind,
    SourceErasureReceipt,
    SuccessorErasureProgress,
    SuccessorErasureState,
    erasure_target_manifest_sha256,
    source_erasure_target_sha256,
)
from rag_engine.governed_memory.runtime.deletion_coordinator import (
    InactiveDeletionCoordinator,
)
from rag_engine.governed_memory.runtime.once_worker import (
    OnceWorkerOutcome,
    OnceWorkerReceipt,
    WorkKind,
)
from rag_engine.governed_memory.runtime.worker_bridge import FairOnceRunner


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OPERATION = UUID("22222222-2222-4222-8222-222222222222")
MESSAGE = UUID("33333333-3333-4333-8333-333333333333")
THREAD = UUID("44444444-4444-4444-8444-444444444444")
LEASE_TOKEN = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
GOVERNED_RECEIPT = "b" * 64
CONVERSATION_RECEIPT = "c" * 64
ROOT = Path(__file__).resolve().parents[2]


def _target() -> ConversationErasureTarget:
    created_at = NOW - timedelta(seconds=1)
    return ConversationErasureTarget(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        message_id=MESSAGE,
        thread_id=THREAD,
        source_created_at=created_at,
        target_sha256=source_erasure_target_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            message_id=MESSAGE,
            thread_id=THREAD,
            source_created_at=created_at,
        ),
    )


TARGETS = (_target(),)
MANIFEST = erasure_target_manifest_sha256(
    owner_user_id=OWNER,
    operation_id=OPERATION,
    targets=TARGETS,
)


def lease(
    state: ConversationErasureState,
    governed_receipt_sha256: str | None = None,
) -> ConversationErasureLease:
    return ConversationErasureLease(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        selector_kind=DeletionSelectorKind.ALL_CONVERSATIONS,
        selector_sha256="a" * 64,
        target_count=1,
        target_manifest_sha256=MANIFEST,
        state=state,
        governed_receipt_sha256=governed_receipt_sha256,
        lease_token=LEASE_TOKEN,
    )


def progress(
    state: SuccessorErasureState,
    *,
    touched: int = 0,
    prepared: int = 0,
    deleted: int = 0,
    governed: str | None = None,
) -> SuccessorErasureProgress:
    return SuccessorErasureProgress(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        state=state,
        target_count=1,
        target_manifest_sha256=MANIFEST,
        touched_claim_count=touched,
        prepared_claim_count=prepared,
        deleted_claim_count=deleted,
        governed_receipt_sha256=governed,
        conversation_receipt_sha256=None,
        last_error_code=None,
    )


def mutation(material: str = "d" * 64) -> DeletionMutationReceipt:
    return DeletionMutationReceipt(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        outcome=DeletionMutationOutcome.APPLIED,
        material_sha256=material,
    )


class FakeConversation:
    def __init__(self, log: list[str], leased: ConversationErasureLease | None):
        self.log = log
        self.leased = leased
        self.failures: list[DeletionRepositoryFailure] = []
        self.failure_attempts: list[
            tuple[DeletionRepositoryFailure, int]
        ] = []
        self.physically_deleted = False

    async def lease_erasure(self, *, worker_id: str, lease_seconds: int):
        self.log.append("conversation.lease")
        return self.leased

    async def read_erasure_targets(self, value):
        self.log.append("conversation.targets")
        return TARGETS

    async def release_erasure_lease(self, value):
        self.log.append("conversation.release")

    async def mark_successor_memory_deleted(
        self, value, *, governed_receipt_sha256: str
    ):
        self.log.append("conversation.mark_memory_deleted")
        self.assert_equal(governed_receipt_sha256, GOVERNED_RECEIPT)

    async def finalize_conversation_erasure(self, value):
        self.log.append("conversation.finalize")
        replay = value.state is (
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
        )
        self.physically_deleted = True
        return ConversationFinalizationReceipt(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            outcome=(
                DeletionMutationOutcome.REPLAYED
                if replay
                else DeletionMutationOutcome.APPLIED
            ),
            receipt_sha256=CONVERSATION_RECEIPT,
            deleted_message_count=1,
            deleted_thread_count=0,
            deleted_attachment_count=2,
            deleted_bridge_row_count=1,
            message_tombstone_count=1,
            thread_tombstone_count=0,
            completed_at=NOW,
        )

    async def acknowledge_erasure_completion(
        self, value, *, conversation_receipt_sha256: str
    ):
        self.log.append("conversation.ack_completion")
        self.assert_equal(conversation_receipt_sha256, CONVERSATION_RECEIPT)

    async def fail_erasure(
        self,
        value,
        *,
        failure: DeletionRepositoryFailure,
        retry_after_seconds: int,
    ):
        self.log.append("conversation.fail")
        self.failures.append(failure)
        self.failure_attempts.append((failure, retry_after_seconds))
        preserve_pending_ack = self.physically_deleted or value.state is (
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
        )
        self.assert_equal(
            retry_after_seconds,
            (
                30
                if preserve_pending_ack
                else (
                    0
                    if failure is DeletionRepositoryFailure.RECEIPT_INVALID
                    else 30
                )
            ),
        )
        if preserve_pending_ack:
            return ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK
        return (
            ConversationErasureState.MANUAL_REVIEW
            if retry_after_seconds == 0
            else ConversationErasureState.RETRYABLE
        )

    @staticmethod
    def assert_equal(left, right):
        if left != right:
            raise AssertionError((left, right))


class FakeSuccessor:
    def __init__(
        self,
        log: list[str],
        progress_rows: list[SuccessorErasureProgress],
        *,
        stage_failure: bool = False,
        stage_contract_failure: bool = False,
        ack_failure: bool = False,
        ack_contract_failure: bool = False,
        touched_claim_count: int = 0,
    ) -> None:
        self.log = log
        self.progress_rows = list(progress_rows)
        self.stage_failure = stage_failure
        self.stage_contract_failure = stage_contract_failure
        self.ack_failure = ack_failure
        self.ack_contract_failure = ack_contract_failure
        self.touched_claim_count = touched_claim_count

    async def stage_source_erasure(self, value):
        self.log.append("successor.register")
        if self.stage_failure:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            )
        if self.stage_contract_failure:
            return DeletionMutationReceipt(
                owner_user_id=UUID("99999999-9999-4999-8999-999999999999"),
                operation_id=OPERATION,
                outcome=DeletionMutationOutcome.APPLIED,
                material_sha256="d" * 64,
            )
        return mutation()

    async def stage_source_erasure_targets(self, value, targets):
        self.log.append("successor.append_targets")
        if targets != TARGETS:
            raise AssertionError(targets)
        return mutation()

    async def seal_source_erasure_targets(self, value):
        self.log.append("successor.seal")
        return mutation()

    async def request_next_source_erasure_claim_deletion(self, value):
        self.log.append("successor.prepare_one_claim")
        return ClaimDeletionStepReceipt(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            outcome=ClaimDeletionStepOutcome.NO_NEW_CLAIM_DELETIONS,
            material_sha256="e" * 64,
        )

    async def read_source_erasure_progress(self, value):
        self.log.append("successor.progress")
        if not self.progress_rows:
            raise AssertionError("unexpected progress read")
        return self.progress_rows.pop(0)

    async def finalize_source_erasure_memory(self, value):
        self.log.append("successor.finalize_memory")
        return mutation(GOVERNED_RECEIPT)

    async def acknowledge_conversation_deleted(
        self, value, *, conversation_receipt_sha256: str
    ):
        self.log.append("successor.ack_conversation")
        if self.ack_failure:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            )
        if self.ack_contract_failure:
            return DeletionMutationReceipt(
                owner_user_id=UUID("99999999-9999-4999-8999-999999999999"),
                operation_id=OPERATION,
                outcome=DeletionMutationOutcome.APPLIED,
                material_sha256="f" * 64,
            )
        if conversation_receipt_sha256 != CONVERSATION_RECEIPT:
            raise AssertionError(conversation_receipt_sha256)
        return mutation("f" * 64)

    async def read_source_erasure_receipt(self, value):
        self.log.append("successor.final_receipt")
        return SourceErasureReceipt(
            operation_id=OPERATION,
            selector_sha256="a" * 64,
            target_manifest_sha256=MANIFEST,
            target_count=1,
            touched_claim_count=self.touched_claim_count,
            claim_deletion_receipt_count=self.touched_claim_count,
            pre_fence_provider_dispatch_count=0,
            governed_absence_sha256=GOVERNED_RECEIPT,
            conversation_receipt_sha256=CONVERSATION_RECEIPT,
            receipt_sha256="f" * 64,
            completed_at=NOW,
        )


class InactiveDeletionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_pass_stops_after_memory_receipt_handoff(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log, lease(ConversationErasureState.GOVERNED_DELETION_PENDING)
        )
        successor = FakeSuccessor(
            log,
            [
                progress(SuccessorErasureState.RECEIVING),
                progress(SuccessorErasureState.FENCED),
                progress(SuccessorErasureState.FENCED),
            ],
        )
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.outcome, DeletionCoordinatorOutcome.PROCESSING)
        self.assertEqual(
            receipt.conversation_state,
            ConversationErasureState.GOVERNED_DELETED,
        )
        self.assertEqual(
            log,
            [
                "conversation.lease",
                "conversation.targets",
                "successor.register",
                "successor.progress",
                "successor.append_targets",
                "successor.seal",
                "successor.progress",
                "successor.prepare_one_claim",
                "successor.progress",
                "successor.finalize_memory",
                "conversation.mark_memory_deleted",
            ],
        )
        self.assertNotIn("conversation.finalize", log)

    async def test_claim_work_is_bounded_to_one_prepare(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log, lease(ConversationErasureState.GOVERNED_DELETION_PENDING)
        )
        successor = FakeSuccessor(
            log,
            [
                progress(SuccessorErasureState.RECEIVING),
                progress(
                    SuccessorErasureState.CLAIM_DELETION_PENDING, touched=1
                ),
                progress(
                    SuccessorErasureState.CLAIM_DELETION_PENDING,
                    touched=1,
                    prepared=1,
                ),
            ],
        )
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.pending_claim_deletions, 1)
        self.assertEqual(log.count("successor.prepare_one_claim"), 1)
        self.assertEqual(log[-1], "conversation.release")

    async def test_governed_deleted_pass_acks_both_databases(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log,
            lease(
                ConversationErasureState.GOVERNED_DELETED,
                GOVERNED_RECEIPT,
            ),
        )
        successor = FakeSuccessor(log, [])
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.outcome, DeletionCoordinatorOutcome.COMPLETED)
        self.assertEqual(
            log,
            [
                "conversation.lease",
                "conversation.targets",
                "conversation.finalize",
                "successor.ack_conversation",
                "successor.final_receipt",
                "conversation.ack_completion",
            ],
        )

    async def test_pending_ack_recovery_does_not_read_deleted_targets(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log,
            lease(
                ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
                GOVERNED_RECEIPT,
            ),
        )
        successor = FakeSuccessor(log, [])
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.outcome, DeletionCoordinatorOutcome.COMPLETED)
        self.assertEqual(
            log,
            [
                "conversation.lease",
                "conversation.finalize",
                "successor.ack_conversation",
                "successor.final_receipt",
                "conversation.ack_completion",
            ],
        )

    async def test_ack_failure_after_chat_delete_retains_pending_ack(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log,
            lease(
                ConversationErasureState.GOVERNED_DELETED,
                GOVERNED_RECEIPT,
            ),
        )
        successor = FakeSuccessor(log, [], ack_failure=True)
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.outcome, DeletionCoordinatorOutcome.RETRYABLE)
        self.assertEqual(
            receipt.conversation_state,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
        )
        self.assertEqual(log[-1], "conversation.fail")

    async def test_operational_failure_is_retryable_and_content_free(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log, lease(ConversationErasureState.GOVERNED_DELETION_PENDING)
        )
        successor = FakeSuccessor(log, [], stage_failure=True)
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(receipt.outcome, DeletionCoordinatorOutcome.RETRYABLE)
        self.assertEqual(
            conversation.failures,
            [DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE],
        )
        self.assertEqual(log[-1], "conversation.fail")

    async def test_contract_failure_before_chat_delete_is_manual_review(
        self,
    ) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log, lease(ConversationErasureState.GOVERNED_DELETION_PENDING)
        )
        successor = FakeSuccessor(
            log,
            [],
            stage_contract_failure=True,
        )
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation,
            successor=successor,
        ).advance_one()
        self.assertEqual(
            receipt.outcome,
            DeletionCoordinatorOutcome.MANUAL_REVIEW,
        )
        self.assertEqual(
            receipt.conversation_state,
            ConversationErasureState.MANUAL_REVIEW,
        )
        self.assertEqual(
            conversation.failure_attempts,
            [(DeletionRepositoryFailure.RECEIPT_INVALID, 0)],
        )
        self.assertFalse(conversation.physically_deleted)

    async def test_contract_failure_after_chat_delete_preserves_pending_ack(
        self,
    ) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log,
            lease(
                ConversationErasureState.GOVERNED_DELETED,
                GOVERNED_RECEIPT,
            ),
        )
        successor = FakeSuccessor(
            log,
            [],
            ack_contract_failure=True,
        )
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation,
            successor=successor,
        ).advance_one()
        self.assertEqual(
            receipt.outcome,
            DeletionCoordinatorOutcome.RETRYABLE,
        )
        self.assertEqual(
            receipt.conversation_state,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
        )
        self.assertEqual(
            receipt.successor_state,
            SuccessorErasureState.MEMORY_DELETED,
        )
        self.assertEqual(
            conversation.failure_attempts,
            [(DeletionRepositoryFailure.RECEIPT_INVALID, 30)],
        )
        self.assertTrue(conversation.physically_deleted)

    async def test_successor_manual_review_is_terminal_not_retry_loop(self) -> None:
        log: list[str] = []
        conversation = FakeConversation(
            log, lease(ConversationErasureState.GOVERNED_DELETION_PENDING)
        )
        successor = FakeSuccessor(
            log,
            [progress(SuccessorErasureState.MANUAL_REVIEW)],
        )
        receipt = await InactiveDeletionCoordinator(
            conversation=conversation, successor=successor
        ).advance_one()
        self.assertEqual(
            receipt.outcome, DeletionCoordinatorOutcome.MANUAL_REVIEW
        )
        self.assertEqual(
            conversation.failures,
            [DeletionRepositoryFailure.RECEIPT_INVALID],
        )
        self.assertEqual(log[-1], "conversation.fail")

    async def test_no_lease_has_no_side_effects(self) -> None:
        log: list[str] = []
        result = await InactiveDeletionCoordinator(
            conversation=FakeConversation(log, None),
            successor=FakeSuccessor(log, []),
        ).advance_one()
        self.assertIsNone(result)
        self.assertEqual(log, ["conversation.lease"])

    async def test_disabled_worker_prioritizes_terminal_erasure_without_pilot(
        self,
    ) -> None:
        deletion_receipt = InactiveDeletionCoordinator._receipt(
            lease(
                ConversationErasureState.GOVERNED_DELETED,
                GOVERNED_RECEIPT,
            ),
            outcome=DeletionCoordinatorOutcome.COMPLETED,
            conversation_state=ConversationErasureState.COMPLETED,
            successor_state=SuccessorErasureState.COMPLETED,
            affected_claim_count=0,
            pending_claim_deletions=0,
        )

        class DeletionLane:
            async def advance_one(self):
                return deletion_receipt

        class PilotMustNotRun:
            async def pilot_ever_started(self):
                raise AssertionError("pilot read preceded privacy erasure")

        result = await FairOnceRunner(
            pilot_repository=PilotMustNotRun(),
            lane_scheduler=object(),
            bridge_worker=object(),
            extraction_worker=object(),
            projection_worker=object(),
            deletion_worker=DeletionLane(),
        ).run_once()
        self.assertEqual(result.outcome, OnceWorkerOutcome.COMPLETED)
        self.assertEqual(result.work_kind, WorkKind.SOURCE_ERASURE)
        self.assertEqual(result.work_id, OPERATION)

    async def test_pending_claim_erasure_runs_only_projection_lane(self) -> None:
        deletion_receipt = InactiveDeletionCoordinator._receipt(
            lease(ConversationErasureState.GOVERNED_DELETION_PENDING),
            outcome=DeletionCoordinatorOutcome.PROCESSING,
            conversation_state=(
                ConversationErasureState.GOVERNED_DELETION_PENDING
            ),
            successor_state=SuccessorErasureState.CLAIM_DELETION_PENDING,
            affected_claim_count=1,
            pending_claim_deletions=1,
        )
        projection_receipt = OnceWorkerReceipt(
            outcome=OnceWorkerOutcome.COMPLETED,
            work_kind=WorkKind.PROJECTION_DELETE,
            work_id=OPERATION,
            qdrant_preflight_sha256="9" * 64,
            receipt_sha256="8" * 64,
        )
        calls: list[str] = []

        class DeletionLane:
            async def advance_one(self):
                calls.append("deletion")
                return deletion_receipt

        class ProjectionLane:
            async def run_once(self):
                calls.append("projection")
                return projection_receipt

        class PilotMustNotRun:
            async def pilot_ever_started(self):
                raise AssertionError("pilot read preceded privacy erasure")

        result = await FairOnceRunner(
            pilot_repository=PilotMustNotRun(),
            lane_scheduler=object(),
            bridge_worker=object(),
            extraction_worker=object(),
            projection_worker=ProjectionLane(),
            deletion_worker=DeletionLane(),
        ).run_once()
        self.assertIs(result, projection_receipt)
        self.assertEqual(calls, ["deletion", "projection"])

    def test_candidate_is_wired_only_into_inactive_worker(self) -> None:
        worker_source = (
            ROOT
            / "rag_engine/governed_memory/runtime/worker_application.py"
        ).read_text(encoding="utf-8")
        self.assertIn("InactiveDeletionCoordinator", worker_source)
        self.assertIn("deletion_worker=", worker_source)
        for relative in (
            "rag_engine/governed_memory/runtime/__init__.py",
            "rag_engine/governed_memory/runtime/application.py",
            "rag_engine/governed_memory/http_api.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("deletion_coordinator", source)
            self.assertNotIn("deletion_postgres", source)


if __name__ == "__main__":
    unittest.main()
