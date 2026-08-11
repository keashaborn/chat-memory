from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.conversation_deletion import (
    DeletionRepositoryFailure,
    require_finalization_receipt_binding,
    validate_erasure_targets,
)
from rag_engine.governed_memory.deletion_contracts import (
    BoundConversationDeletion,
    ConversationErasureLease,
    ConversationErasureState,
    ConversationErasureTarget,
    ConversationDeletionRequestV1,
    ConversationFinalizationReceipt,
    DeletionAuthority,
    DeletionMutationOutcome,
    DeletionSelectorKind,
    deletion_binding_sha256,
    erasure_target_manifest_sha256,
    source_erasure_target_sha256,
)
from rag_engine.governed_memory.runtime.deletion_postgres import (
    PostgresConversationDeletionRepository,
    PostgresSuccessorDeletionRepository,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OPERATION = UUID("33333333-3333-4333-8333-333333333333")
MESSAGE_A = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_B = UUID("55555555-5555-4555-8555-555555555555")
THREAD_A = UUID("66666666-6666-4666-8666-666666666666")
THREAD_B = UUID("77777777-7777-4777-8777-777777777777")
LEASE_TOKEN = UUID("88888888-8888-4888-8888-888888888888")
SESSION = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
AUTH_CONTEXT = "9" * 64


def target(
    message_id: UUID,
    thread_id: UUID,
    created_at: datetime,
) -> ConversationErasureTarget:
    return ConversationErasureTarget(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        message_id=message_id,
        thread_id=thread_id,
        source_created_at=created_at,
        target_sha256=source_erasure_target_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            message_id=message_id,
            thread_id=thread_id,
            source_created_at=created_at,
        ),
    )


def lease_for(
    targets: tuple[ConversationErasureTarget, ...],
    *,
    state: ConversationErasureState = (
        ConversationErasureState.GOVERNED_DELETION_PENDING
    ),
    governed_receipt_sha256: str | None = None,
) -> ConversationErasureLease:
    return ConversationErasureLease(
        owner_user_id=OWNER,
        operation_id=OPERATION,
        selector_kind=DeletionSelectorKind.ALL_CONVERSATIONS,
        selector_sha256="a" * 64,
        target_count=len(targets),
        target_manifest_sha256=erasure_target_manifest_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            targets=targets,
        ),
        state=state,
        governed_receipt_sha256=governed_receipt_sha256,
        lease_token=LEASE_TOKEN,
    )


def bound_command() -> BoundConversationDeletion:
    request = ConversationDeletionRequestV1(
        operation_id=OPERATION,
        selector_kind=DeletionSelectorKind.ALL_CONVERSATIONS,
        confirmation_sha256="8" * 64,
    )
    authority = DeletionAuthority(
        owner_user_id=OWNER,
        actor_id=OWNER,
        session_id=SESSION,
        authentication_manifest_sha256=AUTH_CONTEXT,
    )
    return BoundConversationDeletion(
        authority=authority,
        request=request,
        binding_sha256=deletion_binding_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            request_sha256=request.request_sha256,
        ),
    )


class ConversationDeletionBoundaryTests(unittest.TestCase):
    def test_target_contract_is_content_free(self) -> None:
        self.assertEqual(
            {field.name for field in fields(ConversationErasureTarget)},
            {
                "owner_user_id",
                "operation_id",
                "message_id",
                "thread_id",
                "source_created_at",
                "target_sha256",
            },
        )

    def test_python_validates_db_manifest_without_reinterpreting_selector(self) -> None:
        targets = (
            target(MESSAGE_A, THREAD_A, NOW - timedelta(days=365)),
            target(MESSAGE_B, THREAD_B, NOW + timedelta(days=365)),
        )
        lease = lease_for(targets)
        self.assertEqual(
            validate_erasure_targets(lease, targets),
            lease.target_manifest_sha256,
        )

    def test_target_rows_must_be_sorted_complete_and_hash_exact(self) -> None:
        targets = (
            target(MESSAGE_A, THREAD_A, NOW - timedelta(seconds=2)),
            target(MESSAGE_B, THREAD_B, NOW - timedelta(seconds=1)),
        )
        lease = lease_for(targets)
        with self.assertRaises(ContractViolation) as reversed_rows:
            validate_erasure_targets(lease, tuple(reversed(targets)))
        self.assertEqual(
            reversed_rows.exception.code, "erasure_targets_not_sorted"
        )
        with self.assertRaises(ContractViolation) as incomplete:
            validate_erasure_targets(lease, targets[:1])
        self.assertEqual(
            incomplete.exception.code, "erasure_target_count_mismatch"
        )
        with self.assertRaises(ContractViolation) as tampered:
            replace(targets[0], target_sha256="f" * 64)
        self.assertEqual(
            tampered.exception.code, "erasure_target_sha256_mismatch"
        )

    def test_cross_owner_target_is_rejected_even_with_valid_own_hash(self) -> None:
        item = target(MESSAGE_A, THREAD_A, NOW)
        other_owner = UUID("99999999-9999-4999-8999-999999999999")
        cross_owner = ConversationErasureTarget(
            owner_user_id=other_owner,
            operation_id=OPERATION,
            message_id=MESSAGE_A,
            thread_id=THREAD_A,
            source_created_at=NOW,
            target_sha256=source_erasure_target_sha256(
                owner_user_id=other_owner,
                operation_id=OPERATION,
                message_id=MESSAGE_A,
                thread_id=THREAD_A,
                source_created_at=NOW,
            ),
        )
        lease = lease_for((item,))
        with self.assertRaises(ContractViolation) as raised:
            validate_erasure_targets(lease, (cross_owner,))
        self.assertEqual(
            raised.exception.code, "erasure_target_binding_mismatch"
        )

    def test_finalization_can_report_previously_absent_messages(self) -> None:
        targets = (
            target(MESSAGE_A, THREAD_A, NOW - timedelta(seconds=2)),
            target(MESSAGE_B, THREAD_B, NOW - timedelta(seconds=1)),
        )
        lease = lease_for(
            targets,
            state=ConversationErasureState.GOVERNED_DELETED,
            governed_receipt_sha256="b" * 64,
        )
        receipt = ConversationFinalizationReceipt(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            outcome=DeletionMutationOutcome.APPLIED,
            receipt_sha256="c" * 64,
            deleted_message_count=1,
            deleted_thread_count=1,
            deleted_attachment_count=2,
            deleted_bridge_row_count=1,
            completed_at=NOW,
        )
        self.assertEqual(
            require_finalization_receipt_binding(lease, receipt), receipt
        )
        with self.assertRaises(ContractViolation):
            require_finalization_receipt_binding(
                lease,
                replace(receipt, deleted_message_count=3),
            )
        with self.assertRaises(ContractViolation):
            require_finalization_receipt_binding(
                lease,
                replace(receipt, deleted_bridge_row_count=3),
            )
        empty_thread_receipt = replace(
            receipt,
            deleted_message_count=0,
            deleted_thread_count=1,
            deleted_bridge_row_count=0,
        )
        self.assertEqual(
            require_finalization_receipt_binding(
                lease, empty_thread_receipt
            ),
            empty_thread_receipt,
        )


class _AppendConnection:
    def __init__(self) -> None:
        self.received = 0
        self.page_sizes: list[int] = []

    async def fetchrow(self, query: str, *args: object):
        page_size = len(args[1])
        self.page_sizes.append(page_size)
        self.received += page_size
        return {
            "outcome": "appended",
            "inserted_count": page_size,
            "received_target_count": self.received,
        }


class _RegisterConnection:
    def __init__(self, received_target_count: object) -> None:
        self.received_target_count = received_target_count

    async def fetchrow(self, query: str, *args: object):
        return {
            "outcome": "replayed",
            "state": "receiving",
            "received_target_count": self.received_target_count,
        }


class _Transaction:
    def __init__(self, connection: "_OwnerConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        if self.connection.in_transaction:
            raise AssertionError("nested owner transaction")
        self.connection.in_transaction = True
        self.connection.log.append("transaction.enter")

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.log.append("transaction.exit")
        self.connection.in_transaction = False


class _OwnerConnection:
    def __init__(
        self,
        *,
        forced_owner: str | None = None,
        forced_auth_context: str | None = None,
    ) -> None:
        self.forced_owner = forced_owner
        self.forced_auth_context = forced_auth_context
        self.local_owner: str | None = None
        self.local_auth_context: str | None = None
        self.in_transaction = False
        self.log: list[str] = []
        self.deletion_queries = 0
        self.manifest = erasure_target_manifest_sha256(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            targets=(),
        )

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, query: str, *args: object):
        if not self.in_transaction:
            raise AssertionError("owner context set outside transaction")
        if "'app.user_id'" in query:
            self.local_owner = args[0]
            self.log.append("context.set_owner")
        elif "'app.auth_context_sha256'" in query:
            self.local_auth_context = args[0]
            self.log.append("context.set_auth")
        else:
            raise AssertionError(query)

    async def fetchval(self, query: str, *args: object):
        if "'app.user_id'" in query:
            self.log.append("context.read_owner")
            return (
                self.local_owner
                if self.forced_owner is None
                else self.forced_owner
            )
        if "'app.auth_context_sha256'" in query:
            self.log.append("context.read_auth")
            return (
                self.local_auth_context
                if self.forced_auth_context is None
                else self.forced_auth_context
            )
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: object):
        if not self.in_transaction:
            raise AssertionError("deletion SQL outside owner transaction")
        if self.local_owner != str(OWNER) or self.local_auth_context != AUTH_CONTEXT:
            raise AssertionError("deletion SQL before authority binding")
        self.deletion_queries += 1
        if "begin_source_erasure" in query:
            self.log.append("deletion.begin")
            return {
                "outcome": "fenced",
                "operation_id": OPERATION,
                "state": "fenced",
                "target_count": 0,
                "selector_sha256": "7" * 64,
                "target_manifest_sha256": self.manifest,
            }
        if "read_source_erasure" in query:
            self.log.append("deletion.read")
            return {
                "operation_id": OPERATION,
                "selector_kind": "all_conversations",
                "state": "fenced",
                "target_count": 0,
                "selector_sha256": "7" * 64,
                "target_manifest_sha256": self.manifest,
                "governed_receipt_sha256": None,
                "last_error_code": None,
                "created_at": NOW,
                "completed_at": None,
            }
        raise AssertionError(query)


class PostgresDeletionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_replay_accepts_partial_page_count(self) -> None:
        value = replace(
            lease_for(()),
            target_count=501,
            target_manifest_sha256="6" * 64,
        )
        receipt = await PostgresSuccessorDeletionRepository(
            _RegisterConnection(500)
        ).stage_source_erasure(value)
        self.assertEqual(receipt.outcome, DeletionMutationOutcome.REPLAYED)

    async def test_register_replay_rejects_nonexact_or_overflow_count(
        self,
    ) -> None:
        value = replace(
            lease_for(()),
            target_count=1,
            target_manifest_sha256="6" * 64,
        )
        for received in (True, -1, 2):
            with self.subTest(received=received):
                with self.assertRaises(ContractViolation) as raised:
                    await PostgresSuccessorDeletionRepository(
                        _RegisterConnection(received)
                    ).stage_source_erasure(value)
                self.assertEqual(
                    raised.exception.code,
                    "invalid_register_source_erasure_received_count",
                )

    async def test_owner_request_binds_and_verifies_transaction_context(
        self,
    ) -> None:
        connection = _OwnerConnection()
        status = await PostgresConversationDeletionRepository(
            connection
        ).request_erasure(bound_command())
        self.assertEqual(status.owner_user_id, OWNER)
        self.assertEqual(connection.deletion_queries, 2)
        self.assertEqual(
            connection.log,
            [
                "transaction.enter",
                "context.set_owner",
                "context.set_auth",
                "context.read_owner",
                "context.read_auth",
                "deletion.begin",
                "deletion.read",
                "transaction.exit",
            ],
        )

    async def test_owner_status_read_binds_transaction_context(self) -> None:
        connection = _OwnerConnection()
        status = await PostgresConversationDeletionRepository(
            connection
        ).read_erasure_status(bound_command())
        self.assertIsNotNone(status)
        self.assertEqual(connection.deletion_queries, 1)
        self.assertEqual(connection.log[-2:], ["deletion.read", "transaction.exit"])

    async def test_stale_owner_guc_fails_before_deletion_sql(self) -> None:
        connection = _OwnerConnection(forced_owner=str(OTHER_OWNER))
        with self.assertRaises(ContractViolation) as raised:
            await PostgresConversationDeletionRepository(
                connection
            ).request_erasure(bound_command())
        self.assertEqual(
            raised.exception.code,
            "conversation_deletion_owner_context_mismatch",
        )
        self.assertEqual(connection.deletion_queries, 0)
        self.assertEqual(connection.log[-1], "transaction.exit")

    async def test_stale_auth_guc_fails_before_status_sql(self) -> None:
        connection = _OwnerConnection(forced_auth_context="5" * 64)
        with self.assertRaises(ContractViolation) as raised:
            await PostgresConversationDeletionRepository(
                connection
            ).read_erasure_status(bound_command())
        self.assertEqual(
            raised.exception.code,
            "conversation_deletion_auth_context_mismatch",
        )
        self.assertEqual(connection.deletion_queries, 0)

    async def test_successor_target_append_is_exactly_paged_at_500(self) -> None:
        targets = tuple(
            target(
                UUID(int=index + 1),
                THREAD_A,
                NOW + timedelta(microseconds=index),
            )
            for index in range(501)
        )
        value = lease_for(targets)
        connection = _AppendConnection()
        receipt = await PostgresSuccessorDeletionRepository(
            connection
        ).stage_source_erasure_targets(value, targets)
        self.assertEqual(connection.page_sizes, [500, 1])
        self.assertEqual(receipt.operation_id, OPERATION)
        self.assertEqual(receipt.outcome, DeletionMutationOutcome.APPLIED)

    async def test_failure_after_chat_delete_preserves_pending_ack(self) -> None:
        class Connection:
            async def fetchval(self, query: str, *args: object):
                return "conversation_deleted_pending_ack"

        value = lease_for(
            (),
            state=ConversationErasureState.GOVERNED_DELETED,
            governed_receipt_sha256="b" * 64,
        )
        state = await PostgresConversationDeletionRepository(
            Connection()
        ).fail_erasure(
            value,
            failure=DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE,
            retry_after_seconds=30,
        )
        self.assertEqual(
            state,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
        )


if __name__ == "__main__":
    unittest.main()
