from __future__ import annotations

import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    EligibilityDecision,
)
from rag_engine.governed_memory.repository import IngestSuccessorReceipt
from rag_engine.governed_memory.runtime.once_worker import (
    OnceWorkerOutcome,
    OnceWorkerReceipt,
    WorkKind,
)
from rag_engine.governed_memory.runtime.worker_bridge import (
    BridgeIngestWorker,
    FairOnceRunner,
    PostgresConversationBridge,
)
from rag_engine.governed_memory.worker import ingest_successor_receipt_sha256
from tests.memory._fixtures import (
    BRIDGE_OUTBOX_A,
    CUTOVER,
    EVIDENCE_A,
    JOB_A,
    NOW,
    make_bridge_lease,
    make_ingest_payload,
)


class FakeBridge:
    def __init__(
        self,
        *,
        payload: dict[str, object],
        lease: dict[str, object],
    ) -> None:
        self.payload = payload
        self.lease = lease
        self.claim_calls = 0
        self.read_calls = 0
        self.mark_calls = 0
        self.mark_expectations: list[str] = []
        self.ack_calls: list[dict[str, object]] = []
        self.fail_calls: list[dict[str, object]] = []

    async def claim_one(self) -> dict[str, object] | None:
        self.claim_calls += 1
        return self.lease

    async def read_claimed(
        self,
        lease: dict[str, object],
    ) -> tuple[dict[str, object], object]:
        self.read_calls += 1
        self.assert_same_lease(lease)
        return self.payload, NOW

    def assert_same_lease(self, lease: dict[str, object]) -> None:
        if lease != self.lease:
            raise AssertionError("unexpected lease")

    async def mark_context_review(
        self,
        lease: dict[str, object],
        *,
        expected_outcome: str,
    ) -> None:
        self.assert_same_lease(lease)
        self.mark_calls += 1
        self.mark_expectations.append(expected_outcome)

    async def acknowledge(
        self,
        lease: dict[str, object],
        *,
        decision: str,
        evidence_id: UUID | None,
        job_id: UUID | None,
    ) -> None:
        self.assert_same_lease(lease)
        self.ack_calls.append(
            {
                "decision": decision,
                "evidence_id": evidence_id,
                "job_id": job_id,
            }
        )

    async def fail(
        self,
        lease: dict[str, object],
        *,
        failure_mode: str,
        error_code: str,
        retry_after_seconds: int,
    ) -> None:
        self.assert_same_lease(lease)
        self.fail_calls.append(
            {
                "failure_mode": failure_mode,
                "error_code": error_code,
                "retry_after_seconds": retry_after_seconds,
            }
        )


class FakeSuccessor:
    def __init__(
        self,
        *,
        read_results: list[IngestSuccessorReceipt | None],
        persist_result: IngestSuccessorReceipt | None = None,
        persist_error: Exception | None = None,
    ) -> None:
        self.read_results = list(read_results)
        self.persist_result = persist_result
        self.persist_error = persist_error
        self.read_calls = 0
        self.persist_calls = 0

    async def read_receipt(
        self,
        lease: dict[str, object],
    ) -> IngestSuccessorReceipt | None:
        self.read_calls += 1
        return self.read_results.pop(0)

    async def persist_plan(
        self,
        lease: dict[str, object],
        plan: dict[str, object],
    ) -> IngestSuccessorReceipt | None:
        self.persist_calls += 1
        if self.persist_error is not None:
            raise self.persist_error
        return self.persist_result


def receipt(lease: dict[str, object]) -> IngestSuccessorReceipt:
    owner = UUID(str(lease["owner_user_id"]))
    operation = UUID(str(lease["message_id"]))
    binding = str(lease["source_binding_sha256"])
    return IngestSuccessorReceipt(
        owner_user_id=owner,
        operation_id=operation,
        bridge_source_binding_sha256=binding,
        decision=EligibilityDecision.SEND_EXTERNAL,
        evidence_id=EVIDENCE_A,
        extraction_job_id=JOB_A,
        receipt_sha256=ingest_successor_receipt_sha256(
            owner_user_id=owner,
            operation_id=operation,
            bridge_source_binding_sha256=binding,
            decision=EligibilityDecision.SEND_EXTERNAL.value,
            evidence_id=EVIDENCE_A,
            extraction_job_id=JOB_A,
        ),
    )


class BridgeIngestWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_review_marks_only_no_successor_write_or_ack(self) -> None:
        payload = make_ingest_payload(
            text="Yes, that synthetic preference is still current."
        )
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        bridge = FakeBridge(payload=payload, lease=lease)
        successor = FakeSuccessor(read_results=[None])
        result = await BridgeIngestWorker(
            bridge=bridge,  # type: ignore[arg-type]
            successor=successor,  # type: ignore[arg-type]
        ).run_one()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.work_kind, WorkKind.INGEST)
        self.assertEqual(bridge.mark_calls, 2)
        self.assertEqual(
            bridge.mark_expectations,
            ["marked", "terminal_unresolved"],
        )
        self.assertEqual(bridge.ack_calls, [])
        self.assertEqual(bridge.fail_calls, [])
        self.assertEqual(successor.persist_calls, 0)
        self.assertEqual(successor.read_calls, 1)

    async def test_released_context_count_one_terminalizes_with_one_mark(self) -> None:
        payload = make_ingest_payload(
            text="Yes, that synthetic preference is still current."
        )
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        lease["context_review_count"] = 1
        lease["eligibility_decision"] = EligibilityDecision.REVIEW_CONTEXT.value
        bridge = FakeBridge(payload=payload, lease=lease)
        successor = FakeSuccessor(read_results=[None])
        result = await BridgeIngestWorker(
            bridge=bridge,  # type: ignore[arg-type]
            successor=successor,  # type: ignore[arg-type]
        ).run_one()
        self.assertIsNotNone(result)
        self.assertEqual(bridge.mark_calls, 1)
        self.assertEqual(bridge.mark_expectations, ["terminal_unresolved"])
        self.assertEqual(successor.read_calls, 1)
        self.assertEqual(successor.persist_calls, 0)
        self.assertEqual(bridge.ack_calls, [])
        self.assertEqual(bridge.fail_calls, [])

    async def test_deleted_source_retry_uses_exact_receipt_without_source_read(self) -> None:
        payload = make_ingest_payload()
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        recovered = receipt(lease)
        bridge = FakeBridge(payload=payload, lease=lease)
        successor = FakeSuccessor(read_results=[recovered])
        result = await BridgeIngestWorker(
            bridge=bridge,  # type: ignore[arg-type]
            successor=successor,  # type: ignore[arg-type]
        ).run_one()
        self.assertIsNotNone(result)
        self.assertEqual(successor.persist_calls, 0)
        self.assertEqual(bridge.read_calls, 0)
        self.assertEqual(bridge.mark_calls, 0)
        self.assertEqual(
            bridge.ack_calls,
            [
                {
                    "decision": "send_external",
                    "evidence_id": EVIDENCE_A,
                    "job_id": JOB_A,
                }
            ],
        )

    async def test_successor_write_uncertainty_uses_exact_readback_then_ack(self) -> None:
        payload = make_ingest_payload()
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        recovered = receipt(lease)
        bridge = FakeBridge(payload=payload, lease=lease)
        successor = FakeSuccessor(
            read_results=[None, recovered],
            persist_error=RuntimeError("synthetic-write-uncertainty"),
        )
        result = await BridgeIngestWorker(
            bridge=bridge,  # type: ignore[arg-type]
            successor=successor,  # type: ignore[arg-type]
        ).run_one()
        self.assertIsNotNone(result)
        self.assertEqual(successor.persist_calls, 1)
        self.assertEqual(successor.read_calls, 2)
        self.assertEqual(len(bridge.ack_calls), 1)
        self.assertEqual(bridge.fail_calls, [])


class PostgresConversationBridgeTests(unittest.IsolatedAsyncioTestCase):
    class Connection:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
            self.calls.append((sql, args))
            return self.row

    async def test_terminal_call_rejects_nonterminal_marked_receipt(self) -> None:
        payload = make_ingest_payload(
            text="Yes, that synthetic preference is still current."
        )
        lease = make_bridge_lease(payload, ingest_after=CUTOVER)
        lease["context_review_count"] = 1
        connection = self.Connection(
            {"outcome": "marked", "context_review_count": 1}
        )
        bridge = PostgresConversationBridge(
            connection,
            worker_id="governed-memory-pilot-worker-1",
        )

        with self.assertRaisesRegex(
            ContractViolation,
            "invalid_bridge_context_review_receipt",
        ):
            await bridge.mark_context_review(
                lease,
                expected_outcome="terminal_unresolved",
            )

        self.assertEqual(len(connection.calls), 1)


class BridgePriorityTests(unittest.IsolatedAsyncioTestCase):
    class Pilot:
        async def pilot_ever_started(self) -> bool:
            return True

    class Scheduler:
        def __init__(self) -> None:
            self.cursor = 0
            self.calls = 0

        async def next_worker_lane(self) -> str:
            lanes = ("bridge", "extraction", "projection")
            lane = lanes[self.cursor % len(lanes)]
            self.cursor += 1
            self.calls += 1
            return lane

    class RejectedPilot:
        async def pilot_ever_started(self) -> bool:
            return False

    class AlwaysBridge:
        def __init__(self, *, failure: Exception | None = None) -> None:
            self.calls = 0
            self.failure = failure

        async def run_one(self) -> OnceWorkerReceipt:
            self.calls += 1
            if self.failure is not None:
                raise self.failure
            return OnceWorkerReceipt(
                outcome=OnceWorkerOutcome.COMPLETED,
                work_kind=WorkKind.INGEST,
                work_id=BRIDGE_OUTBOX_A,
                qdrant_preflight_sha256=None,
                receipt_sha256="a" * 64,
            )

    class AlwaysLane:
        def __init__(self, kind: WorkKind) -> None:
            self.kind = kind
            self.calls = 0

        async def run_once(self) -> OnceWorkerReceipt:
            self.calls += 1
            return OnceWorkerReceipt(
                outcome=OnceWorkerOutcome.COMPLETED,
                work_kind=self.kind,
                work_id=UUID(int=self.calls),
                qdrant_preflight_sha256=None,
                receipt_sha256="b" * 64,
            )

    async def test_bridge_turn_prevents_downstream_claim(self) -> None:
        scheduler = self.Scheduler()
        bridge = self.AlwaysBridge()
        extraction = self.AlwaysLane(WorkKind.EXTRACTION)
        projection = self.AlwaysLane(WorkKind.PROJECTION_UPSERT)
        result = await FairOnceRunner(
            pilot_repository=self.Pilot(),
            lane_scheduler=scheduler,
            bridge_worker=bridge,  # type: ignore[arg-type]
            extraction_worker=extraction,
            projection_worker=projection,
        ).run_once()
        self.assertEqual(result.work_kind, WorkKind.INGEST)
        self.assertEqual(bridge.calls, 1)
        self.assertEqual(extraction.calls, 0)
        self.assertEqual(projection.calls, 0)
        self.assertEqual(scheduler.calls, 1)

    async def test_pilot_rejection_precedes_scheduler_and_every_lane(self) -> None:
        scheduler = self.Scheduler()
        bridge = self.AlwaysBridge()
        extraction = self.AlwaysLane(WorkKind.EXTRACTION)
        projection = self.AlwaysLane(WorkKind.PROJECTION_UPSERT)
        with self.assertRaisesRegex(ContractViolation, "pilot_never_started"):
            await FairOnceRunner(
                pilot_repository=self.RejectedPilot(),
                lane_scheduler=scheduler,
                bridge_worker=bridge,  # type: ignore[arg-type]
                extraction_worker=extraction,
                projection_worker=projection,
            ).run_once()
        self.assertEqual(scheduler.calls, 0)
        self.assertEqual(bridge.calls, 0)
        self.assertEqual(extraction.calls, 0)
        self.assertEqual(projection.calls, 0)

    async def test_continuous_all_lane_arrivals_make_finite_equal_progress(self) -> None:
        scheduler = self.Scheduler()
        bridge = self.AlwaysBridge()
        extraction = self.AlwaysLane(WorkKind.EXTRACTION)
        projection = self.AlwaysLane(WorkKind.PROJECTION_UPSERT)
        outcomes: list[WorkKind | None] = []
        for _ in range(9):
            runner = FairOnceRunner(
                pilot_repository=self.Pilot(),
                lane_scheduler=scheduler,
                bridge_worker=bridge,  # type: ignore[arg-type]
                extraction_worker=extraction,
                projection_worker=projection,
            )
            outcomes.append((await runner.run_once()).work_kind)
        self.assertEqual(
            outcomes,
            [
                WorkKind.INGEST,
                WorkKind.EXTRACTION,
                WorkKind.PROJECTION_UPSERT,
            ]
            * 3,
        )
        self.assertEqual((bridge.calls, extraction.calls, projection.calls), (3, 3, 3))
        self.assertEqual(scheduler.calls, 9)

    async def test_crash_after_cursor_advance_does_not_repeat_same_first_lane(self) -> None:
        scheduler = self.Scheduler()
        crashing_bridge = self.AlwaysBridge(
            failure=RuntimeError("synthetic-crash-after-cursor")
        )
        extraction = self.AlwaysLane(WorkKind.EXTRACTION)
        projection = self.AlwaysLane(WorkKind.PROJECTION_UPSERT)
        with self.assertRaisesRegex(RuntimeError, "synthetic-crash-after-cursor"):
            await FairOnceRunner(
                pilot_repository=self.Pilot(),
                lane_scheduler=scheduler,
                bridge_worker=crashing_bridge,  # type: ignore[arg-type]
                extraction_worker=extraction,
                projection_worker=projection,
            ).run_once()
        next_result = await FairOnceRunner(
            pilot_repository=self.Pilot(),
            lane_scheduler=scheduler,
            bridge_worker=self.AlwaysBridge(),  # type: ignore[arg-type]
            extraction_worker=extraction,
            projection_worker=projection,
        ).run_once()
        self.assertEqual(next_result.work_kind, WorkKind.EXTRACTION)
        self.assertEqual(scheduler.calls, 2)


if __name__ == "__main__":
    unittest.main()
