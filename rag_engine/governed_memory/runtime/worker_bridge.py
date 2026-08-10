from __future__ import annotations

"""Two-database, one-item conversation bridge runtime composition."""

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import UUID

from ..auth import ActorRole, ActorScope, VerifiedActor
from ..contracts import (
    ContractViolation,
    EligibilityDecision,
    canonical_sha256,
    require_utc,
    require_uuid,
)
from ..repository import IngestSuccessorReceipt
from ..worker import BRIDGE_LEASE_FIELDS, process_ingest_item
from .once_worker import (
    OnceWorkerOutcome,
    OnceWorkerReceipt,
    WorkKind,
)


WORKER_AUTHENTICATION_MANIFEST_SHA256 = sha256(
    b"governed-memory-successor-database-worker-role-v1"
).hexdigest()
WORKER_ACTOR_ID = UUID("3eab45b9-7e5a-4dd4-9dd6-c4b8d0d7302c")

_LEASE_SQL = (
    "SELECT * FROM memory_ingest_private.lease_memory_ingest("
    "$1::text,1,$2::integer)"
)
_READ_SQL = (
    "SELECT * FROM memory_ingest_private.read_leased_chat_log_message("
    "$1::uuid,$2::uuid)"
)
_TRANSACTION_TIME_SQL = "SELECT pg_catalog.transaction_timestamp()"
_MARK_CONTEXT_SQL = (
    "SELECT * FROM memory_ingest_private.mark_memory_ingest_context_review("
    "$1::uuid,$2::uuid)"
)
_ACK_SQL = (
    "SELECT memory_ingest_private.ack_memory_ingest("
    "$1::uuid,$2::uuid,$3::text,$4::uuid,$5::uuid)"
)
_FAIL_SQL = (
    "SELECT memory_ingest_private.fail_memory_ingest("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::integer)"
)
_PERSIST_INGEST_SQL = (
    "SELECT * FROM memory_private.record_selected_evidence("
    "$1::uuid,$2::uuid,$3::text,$4::uuid,$5::uuid,$6::uuid,$7::text,"
    "$8::text,$9::text,$10::integer,$11::integer,$12::uuid,$13::text,"
    "$14::timestamptz,$15::text,$16::text,$17::text)"
)
_READ_RECEIPT_SQL = (
    "SELECT * FROM memory_private.read_ingest_receipt("
    "$1::uuid,$2::uuid,$3::text)"
)

BRIDGE_MESSAGE_FIELDS = BRIDGE_LEASE_FIELDS + ("role", "content")
ContextReviewOutcome = Literal["marked", "terminal_unresolved"]


def _uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ContractViolation(code) from error
    return require_uuid(parsed, code)


def _one_row(values: Any, code: str) -> dict[str, Any] | None:
    rows = [dict(value) for value in values]
    if len(rows) > 1:
        raise ContractViolation(code)
    return rows[0] if rows else None


def _receipt(work_id: UUID) -> OnceWorkerReceipt:
    material = {
        "outcome": OnceWorkerOutcome.COMPLETED.value,
        "qdrant_preflight_sha256": None,
        "work_id": str(work_id),
        "work_kind": WorkKind.INGEST.value,
    }
    return OnceWorkerReceipt(
        outcome=OnceWorkerOutcome.COMPLETED,
        qdrant_preflight_sha256=None,
        receipt_sha256=canonical_sha256(
            "governed_memory.once_worker_receipt",
            material,
        ),
        work_id=work_id,
        work_kind=WorkKind.INGEST,
    )


def _successor_receipt_mapping(
    receipt: IngestSuccessorReceipt,
) -> dict[str, object]:
    return {
        "owner_user_id": str(receipt.owner_user_id),
        "operation_id": str(receipt.operation_id),
        "bridge_source_binding_sha256": (
            receipt.bridge_source_binding_sha256
        ),
        "decision": receipt.decision.value,
        "evidence_id": str(receipt.evidence_id),
        "extraction_job_id": str(receipt.extraction_job_id),
        "receipt_sha256": receipt.receipt_sha256,
    }


class ConversationBridge(Protocol):
    async def claim_one(self) -> Mapping[str, Any] | None: ...

    async def read_claimed(
        self,
        lease: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], datetime]: ...

    async def mark_context_review(
        self,
        lease: Mapping[str, Any],
        *,
        expected_outcome: ContextReviewOutcome,
    ) -> None: ...

    async def acknowledge(
        self,
        lease: Mapping[str, Any],
        *,
        decision: str,
        evidence_id: UUID | None,
        job_id: UUID | None,
    ) -> None: ...

    async def fail(
        self,
        lease: Mapping[str, Any],
        *,
        failure_mode: str,
        error_code: str,
        retry_after_seconds: int,
    ) -> None: ...


class SuccessorIngest(Protocol):
    async def read_receipt(
        self,
        lease: Mapping[str, Any],
    ) -> IngestSuccessorReceipt | None: ...

    async def persist_plan(
        self,
        lease: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> IngestSuccessorReceipt | None: ...


class PostgresConversationBridge:
    """Exact SECURITY DEFINER-only access to the existing conversation DB."""

    def __init__(
        self,
        connection: Any,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> None:
        if connection is None:
            raise ContractViolation("worker_bridge_connection_required")
        if worker_id != "governed-memory-pilot-worker-1":
            raise ContractViolation("worker_id_mismatch")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ContractViolation("worker_lease_seconds_invalid")
        self._connection = connection
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def claim_one(self) -> Mapping[str, Any] | None:
        row = _one_row(
            await self._connection.fetch(
                _LEASE_SQL,
                self._worker_id,
                self._lease_seconds,
            ),
            "bridge_claim_cardinality_violation",
        )
        if row is None:
            return None
        if tuple(row) != BRIDGE_LEASE_FIELDS:
            raise ContractViolation("invalid_bridge_lease_row")
        return row

    async def read_claimed(
        self,
        lease: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], datetime]:
        outbox_id = _uuid(lease["outbox_id"], "invalid_bridge_outbox_id")
        lease_token = _uuid(lease["lease_token"], "invalid_bridge_lease_token")
        row = await self._connection.fetchrow(
            _READ_SQL,
            outbox_id,
            lease_token,
        )
        message = dict(row) if row is not None else {}
        if tuple(message) != BRIDGE_MESSAGE_FIELDS:
            raise ContractViolation("invalid_bridge_message_row")
        if any(message[field] != lease[field] for field in BRIDGE_LEASE_FIELDS):
            raise ContractViolation("bridge_message_lease_mismatch")
        transaction_time = await self._connection.fetchval(_TRANSACTION_TIME_SQL)
        if not isinstance(transaction_time, datetime):
            raise ContractViolation("invalid_bridge_transaction_time")
        payload = {
            "owner_user_id": message["owner_user_id"],
            "thread_id": message["thread_id"],
            "message_id": message["message_id"],
            "role": message["role"],
            "created_at": message["source_created_at"],
            "content_sha256": message["content_sha256"],
            "content": message["content"],
            "attachment_ids": [],
            "exchange_id": message["exchange_id"],
            "window_id": message["window_id"],
            "window_ordinal": message["window_ordinal"],
            "window_sha256": message["window_sha256"],
        }
        return payload, require_utc(
            transaction_time,
            "invalid_bridge_transaction_time",
        )

    async def mark_context_review(
        self,
        lease: Mapping[str, Any],
        *,
        expected_outcome: ContextReviewOutcome,
    ) -> None:
        row = await self._connection.fetchrow(
            _MARK_CONTEXT_SQL,
            _uuid(lease["outbox_id"], "invalid_bridge_outbox_id"),
            _uuid(lease["lease_token"], "invalid_bridge_lease_token"),
        )
        value = dict(row) if row is not None else {}
        if tuple(value) != ("outcome", "context_review_count") or (
            value["outcome"] != expected_outcome
            or value["context_review_count"] != 1
        ):
            raise ContractViolation("invalid_bridge_context_review_receipt")

    async def acknowledge(
        self,
        lease: Mapping[str, Any],
        *,
        decision: str,
        evidence_id: UUID | None,
        job_id: UUID | None,
    ) -> None:
        outcome = await self._connection.fetchval(
            _ACK_SQL,
            _uuid(lease["outbox_id"], "invalid_bridge_outbox_id"),
            _uuid(lease["lease_token"], "invalid_bridge_lease_token"),
            decision,
            evidence_id,
            job_id,
        )
        if outcome not in {"completed", "skipped", "replayed"}:
            raise ContractViolation("invalid_bridge_acknowledgment_receipt")

    async def fail(
        self,
        lease: Mapping[str, Any],
        *,
        failure_mode: str,
        error_code: str,
        retry_after_seconds: int,
    ) -> None:
        outcome = await self._connection.fetchval(
            _FAIL_SQL,
            _uuid(lease["outbox_id"], "invalid_bridge_outbox_id"),
            _uuid(lease["lease_token"], "invalid_bridge_lease_token"),
            failure_mode,
            error_code,
            retry_after_seconds,
        )
        if outcome not in {"retryable", "failed_terminal", "replayed"}:
            raise ContractViolation("invalid_bridge_failure_receipt")


class PostgresSuccessorIngest:
    """Exact successor intake RPC adapter; no direct table access."""

    def __init__(self, connection: Any) -> None:
        if connection is None:
            raise ContractViolation("worker_successor_connection_required")
        self._connection = connection

    @staticmethod
    def _receipt(
        lease: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> IngestSuccessorReceipt:
        return IngestSuccessorReceipt(
            owner_user_id=_uuid(
                lease["owner_user_id"],
                "invalid_ingest_receipt_owner",
            ),
            operation_id=_uuid(
                lease["message_id"],
                "invalid_ingest_receipt_operation",
            ),
            bridge_source_binding_sha256=str(lease["source_binding_sha256"]),
            decision=EligibilityDecision.SEND_EXTERNAL,
            evidence_id=_uuid(
                row["evidence_id"],
                "invalid_ingest_receipt_evidence",
            ),
            extraction_job_id=_uuid(
                row["extraction_job_id"],
                "invalid_ingest_receipt_job",
            ),
            receipt_sha256=str(row["ingest_receipt_sha256"]),
        )

    async def read_receipt(
        self,
        lease: Mapping[str, Any],
    ) -> IngestSuccessorReceipt | None:
        row = await self._connection.fetchrow(
            _READ_RECEIPT_SQL,
            _uuid(lease["owner_user_id"], "invalid_ingest_receipt_owner"),
            _uuid(lease["message_id"], "invalid_ingest_receipt_operation"),
            lease["source_binding_sha256"],
        )
        if row is None:
            return None
        value = dict(row)
        if tuple(value) != (
            "evidence_id",
            "extraction_job_id",
            "ingest_receipt_sha256",
        ):
            raise ContractViolation("invalid_ingest_receipt_row")
        return self._receipt(lease, value)

    async def persist_plan(
        self,
        lease: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> IngestSuccessorReceipt | None:
        try:
            decision = EligibilityDecision(str(plan["decision"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ContractViolation("invalid_ingest_persistence_plan") from error
        selected = plan.get("selected_evidence")
        if decision is EligibilityDecision.SEND_EXTERNAL:
            if not isinstance(selected, Mapping):
                raise ContractViolation("invalid_ingest_persistence_plan")
            selected_sha256 = selected["selected_sha256"]
            selected_start_utf8 = selected["start_utf8"]
            selected_end_utf8 = selected["end_utf8"]
            context_message_id = (
                _uuid(selected["context_message_id"], "invalid_context_message_id")
                if selected["context_message_id"] is not None
                else None
            )
            context_sha256 = selected["context_sha256"]
            review_excerpt = selected["selected_text"]
        else:
            if selected is not None:
                raise ContractViolation("invalid_ingest_persistence_plan")
            selected_sha256 = None
            selected_start_utf8 = None
            selected_end_utf8 = None
            context_message_id = None
            context_sha256 = None
            review_excerpt = None
        row = await self._connection.fetchrow(
            _PERSIST_INGEST_SQL,
            _uuid(lease["owner_user_id"], "invalid_ingest_owner"),
            _uuid(lease["message_id"], "invalid_ingest_operation"),
            lease["source_binding_sha256"],
            _uuid(lease["message_id"], "invalid_ingest_message"),
            _uuid(lease["thread_id"], "invalid_ingest_thread"),
            _uuid(lease["window_id"], "invalid_ingest_window"),
            lease["window_sha256"],
            lease["content_sha256"],
            selected_sha256,
            selected_start_utf8,
            selected_end_utf8,
            context_message_id,
            context_sha256,
            lease["source_created_at"],
            decision.value,
            lease["policy_sha256"],
            review_excerpt,
        )
        value = dict(row) if row is not None else {}
        if tuple(value) != (
            "outcome",
            "evidence_id",
            "extraction_job_id",
            "ingest_receipt_sha256",
        ):
            raise ContractViolation("invalid_ingest_persistence_receipt")
        if decision is EligibilityDecision.SEND_EXTERNAL:
            if value["outcome"] not in {"accepted", "replayed"}:
                raise ContractViolation("invalid_ingest_persistence_receipt")
            return self._receipt(lease, value)
        expected = (
            {"context_required", "context_required_replayed"}
            if decision is EligibilityDecision.REVIEW_CONTEXT
            else {"terminal_decision", "terminal_decision_replayed"}
        )
        if value["outcome"] not in expected or any(
            value[field] is not None
            for field in (
                "evidence_id",
                "extraction_job_id",
                "ingest_receipt_sha256",
            )
        ):
            raise ContractViolation("invalid_ingest_persistence_receipt")
        return None


class BridgeIngestWorker:
    """Claim and advance at most one post-cutover bridge item."""

    def __init__(
        self,
        *,
        bridge: ConversationBridge,
        successor: SuccessorIngest,
    ) -> None:
        self._bridge = bridge
        self._successor = successor

    async def _fail_read(
        self,
        lease: Mapping[str, Any],
        error: Exception,
    ) -> None:
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(error, ContractViolation) or sqlstate == "23514":
            mode = "failed_terminal"
            code = (
                "source_binding_mismatch"
                if "binding" in str(error) or sqlstate == "23514"
                else "bridge_contract_violation"
            )
            retry = 0
        else:
            mode = "retryable"
            code = "conversation_read_failed"
            retry = 5
        await self._bridge.fail(
            lease,
            failure_mode=mode,
            error_code=code,
            retry_after_seconds=retry,
        )

    async def _ack_receipt(
        self,
        lease: Mapping[str, Any],
        receipt: IngestSuccessorReceipt,
    ) -> None:
        await self._bridge.acknowledge(
            lease,
            decision=EligibilityDecision.SEND_EXTERNAL.value,
            evidence_id=receipt.evidence_id,
            job_id=receipt.extraction_job_id,
        )

    async def run_one(self) -> OnceWorkerReceipt | None:
        lease = await self._bridge.claim_one()
        if lease is None:
            return None
        work_id = _uuid(lease["outbox_id"], "invalid_bridge_outbox_id")
        try:
            recovered = await self._successor.read_receipt(lease)
        except ContractViolation as error:
            await self._bridge.fail(
                lease,
                failure_mode="failed_terminal",
                error_code="successor_receipt_mismatch",
                retry_after_seconds=0,
            )
            raise error
        except Exception:
            recovered = None
        if recovered is not None:
            await self._ack_receipt(lease, recovered)
            return _receipt(work_id)

        try:
            payload, transaction_time = await self._bridge.read_claimed(lease)
        except Exception as error:
            await self._fail_read(lease, error)
            raise

        owner = _uuid(lease["owner_user_id"], "invalid_bridge_lease_owner")
        actor = VerifiedActor(
            owner_user_id=owner,
            actor_id=WORKER_ACTOR_ID,
            session_id=_uuid(
                lease["lease_token"],
                "invalid_bridge_lease_token",
            ),
            role=ActorRole.WORKER,
            scopes=(ActorScope.PROCESS_MEMORY_INGEST,),
            authentication_manifest_sha256=(
                WORKER_AUTHENTICATION_MANIFEST_SHA256
            ),
            authenticated_at=transaction_time,
        )

        try:
            plan = process_ingest_item(
                payload,
                lease_envelope=lease,
                actor=actor,
                expected_owner_user_id=owner,
                transaction_time=transaction_time,
            )
        except ContractViolation as error:
            code = (
                "source_binding_mismatch"
                if "binding" in str(error)
                else "eligibility_contract_violation"
            )
            await self._bridge.fail(
                lease,
                failure_mode="failed_terminal",
                error_code=code,
                retry_after_seconds=0,
            )
            raise

        decision = str(plan["decision"])
        if decision == EligibilityDecision.REVIEW_CONTEXT.value:
            # The Phase 6B pilot has no bounded assistant-context RPC.  The
            # bridge terminalizes at count one.  A fresh item needs two marks;
            # a re-leased item recovered after the first mark needs one.
            # Both paths use the current exact lease after one content-free
            # successor receipt lookup. They perform zero successor writes,
            # provider, embedding, or vector I/O.
            if lease["context_review_count"] == 0:
                await self._bridge.mark_context_review(
                    lease,
                    expected_outcome="marked",
                )
            await self._bridge.mark_context_review(
                lease,
                expected_outcome="terminal_unresolved",
            )
            return _receipt(work_id)

        try:
            receipt = await self._successor.persist_plan(lease, plan)
        except Exception as original_error:
            try:
                recovered = await self._successor.read_receipt(lease)
            except ContractViolation:
                await self._bridge.fail(
                    lease,
                    failure_mode="failed_terminal",
                    error_code="successor_receipt_mismatch",
                    retry_after_seconds=0,
                )
                raise
            except Exception:
                recovered = None
            if (
                recovered is not None
                and decision == EligibilityDecision.SEND_EXTERNAL.value
            ):
                recovery_plan = process_ingest_item(
                    payload,
                    lease_envelope=lease,
                    actor=actor,
                    expected_owner_user_id=owner,
                    transaction_time=transaction_time,
                    successor_ingest_receipt=(
                        _successor_receipt_mapping(recovered)
                    ),
                )
                if recovery_plan["effects"] != [
                    "ack_existing_memory_ingest"
                ]:
                    raise ContractViolation("invalid_ingest_recovery_plan")
                await self._ack_receipt(lease, recovered)
                return _receipt(work_id)
            await self._bridge.fail(
                lease,
                failure_mode="retryable",
                error_code="successor_write_failed",
                retry_after_seconds=5,
            )
            raise original_error

        if decision == EligibilityDecision.SEND_EXTERNAL.value:
            if not isinstance(receipt, IngestSuccessorReceipt):
                raise ContractViolation("invalid_ingest_persistence_receipt")
            evidence_id = receipt.evidence_id
            job_id = receipt.extraction_job_id
        else:
            if receipt is not None:
                raise ContractViolation("invalid_ingest_persistence_receipt")
            evidence_id = None
            job_id = None
        await self._bridge.acknowledge(
            lease,
            decision=decision,
            evidence_id=evidence_id,
            job_id=job_id,
        )
        return _receipt(work_id)


class FairOnceRunner:
    """Persistently rotate the first lane and claim at most one item."""

    _LANES = ("bridge", "extraction", "projection")

    def __init__(
        self,
        *,
        pilot_repository: Any,
        lane_scheduler: Any,
        bridge_worker: BridgeIngestWorker,
        extraction_worker: Any,
        projection_worker: Any,
    ) -> None:
        self._pilot_repository = pilot_repository
        self._lane_scheduler = lane_scheduler
        self._bridge_worker = bridge_worker
        self._extraction_worker = extraction_worker
        self._projection_worker = projection_worker

    async def run_once(self) -> OnceWorkerReceipt:
        if await self._pilot_repository.pilot_ever_started() is not True:
            raise ContractViolation("pilot_never_started")
        first_lane = await self._lane_scheduler.next_worker_lane()
        if first_lane not in self._LANES:
            raise ContractViolation("invalid_worker_lane")
        start = self._LANES.index(first_lane)
        no_work_receipt: OnceWorkerReceipt | None = None
        for offset in range(len(self._LANES)):
            lane = self._LANES[(start + offset) % len(self._LANES)]
            if lane == "bridge":
                receipt = await self._bridge_worker.run_one()
                if receipt is not None:
                    return receipt
                continue
            worker = (
                self._extraction_worker
                if lane == "extraction"
                else self._projection_worker
            )
            receipt = await worker.run_once()
            if receipt.outcome is OnceWorkerOutcome.COMPLETED:
                return receipt
            if receipt.outcome is not OnceWorkerOutcome.NO_WORK:
                raise ContractViolation("invalid_worker_lane_receipt")
            no_work_receipt = receipt
        if no_work_receipt is None:
            raise ContractViolation("invalid_worker_lane_receipt")
        return no_work_receipt


__all__ = [
    "BRIDGE_MESSAGE_FIELDS",
    "BridgeIngestWorker",
    "FairOnceRunner",
    "PostgresConversationBridge",
    "PostgresSuccessorIngest",
    "WORKER_ACTOR_ID",
    "WORKER_AUTHENTICATION_MANIFEST_SHA256",
]
