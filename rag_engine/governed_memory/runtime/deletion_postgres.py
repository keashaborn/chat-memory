from __future__ import annotations

"""Static-SQL adapters for the inactive deletion coordinator.

These classes do not open connections. The worker adapters are composed only
inside the mode-off candidate worker; the owner request adapter remains
unmounted. Supplied connections must already carry the narrowly scoped
conversation request/worker or successor worker database role.
"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Mapping
from uuid import UUID

from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_sha256,
    require_utc,
    require_uuid,
)
from ..conversation_deletion import (
    DeletionRepositoryError,
    DeletionRepositoryFailure,
    require_request_status_binding,
    validate_erasure_targets,
)
from ..deletion_contracts import (
    BoundConversationDeletion,
    ClaimDeletionStepOutcome,
    ClaimDeletionStepReceipt,
    ConversationErasureLease,
    ConversationErasureState,
    ConversationErasureStatus,
    ConversationErasureTarget,
    ConversationFinalizationReceipt,
    DeletionMutationOutcome,
    DeletionMutationReceipt,
    DeletionSelectorKind,
    SourceErasureReceipt,
    SuccessorErasureProgress,
    SuccessorErasureState,
)


_BEGIN_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_ingest_private.begin_source_erasure("
    "$1::uuid,$2::text,$3::uuid,$4::uuid,$5::integer,$6::text)"
)
_READ_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_ingest_private.read_source_erasure($1::uuid)"
)
_LEASE_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_ingest_private.lease_source_erasure("
    "$1::text,$2::integer)"
)
_READ_SOURCE_ERASURE_TARGETS_SQL = (
    "SELECT * FROM memory_ingest_private.read_source_erasure_targets("
    "$1::uuid,$2::uuid,$3::timestamptz,$4::uuid,$5::integer)"
)
_RELEASE_SOURCE_ERASURE_LEASE_SQL = (
    "SELECT memory_ingest_private.release_source_erasure_lease("
    "$1::uuid,$2::uuid)"
)
_MARK_SOURCE_ERASURE_GOVERNED_DELETED_SQL = (
    "SELECT memory_ingest_private.mark_source_erasure_governed_deleted("
    "$1::uuid,$2::uuid,$3::text,$4::integer,$5::text)"
)
_FINALIZE_CONVERSATION_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_ingest_private.finalize_source_erasure("
    "$1::uuid,$2::uuid,$3::text)"
)
_ACK_CONVERSATION_SOURCE_ERASURE_COMPLETION_SQL = (
    "SELECT memory_ingest_private.ack_source_erasure_completion("
    "$1::uuid,$2::uuid,$3::text)"
)
_FAIL_SOURCE_ERASURE_SQL = (
    "SELECT memory_ingest_private.fail_source_erasure("
    "$1::uuid,$2::uuid,$3::text,$4::text)"
)

_SET_OWNER_CONTEXT_SQL = (
    "SELECT pg_catalog.set_config('app.user_id',$1::text,true)"
)
_SET_AUTH_CONTEXT_SQL = (
    "SELECT pg_catalog.set_config("
    "'app.auth_context_sha256',$1::text,true)"
)
_READ_OWNER_CONTEXT_SQL = (
    "SELECT pg_catalog.current_setting('app.user_id',true)"
)
_READ_AUTH_CONTEXT_SQL = (
    "SELECT pg_catalog.current_setting('app.auth_context_sha256',true)"
)

_REGISTER_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_private.register_source_erasure("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::integer,$6::text)"
)
_APPEND_SOURCE_ERASURE_TARGETS_SQL = (
    "SELECT * FROM memory_private.append_source_erasure_targets("
    "$1::uuid,$2::uuid[],$3::uuid[],$4::timestamptz[],$5::text[])"
)
_SEAL_SOURCE_ERASURE_SQL = (
    "SELECT * FROM memory_private.seal_source_erasure($1::uuid)"
)
_PREPARE_SOURCE_ERASURE_CLAIM_DELETIONS_SQL = (
    "SELECT * FROM memory_private.prepare_source_erasure_claim_deletions("
    "$1::uuid,$2::integer)"
)
_READ_SUCCESSOR_SOURCE_ERASURE_PROGRESS_SQL = (
    "SELECT * FROM memory_private.read_source_erasure_progress($1::uuid)"
)
_FINALIZE_SOURCE_ERASURE_MEMORY_SQL = (
    "SELECT * FROM memory_private.finalize_source_erasure_memory($1::uuid)"
)
_ACK_SOURCE_ERASURE_CONVERSATION_DELETED_SQL = (
    "SELECT * FROM memory_private.ack_source_erasure_conversation_deleted("
    "$1::uuid,$2::text)"
)
_READ_SUCCESSOR_SOURCE_ERASURE_RECEIPT_SQL = (
    "SELECT * FROM memory_private.read_source_erasure_receipt($1::uuid)"
)

BEGIN_SOURCE_ERASURE_FIELDS = (
    "outcome",
    "operation_id",
    "state",
    "target_count",
    "selector_sha256",
    "target_manifest_sha256",
)
READ_SOURCE_ERASURE_FIELDS = (
    "operation_id",
    "selector_kind",
    "state",
    "target_count",
    "selector_sha256",
    "target_manifest_sha256",
    "governed_receipt_sha256",
    "last_error_code",
    "created_at",
    "completed_at",
)
LEASE_SOURCE_ERASURE_FIELDS = (
    "operation_id",
    "owner_user_id",
    "selector_kind",
    "selector_sha256",
    "target_count",
    "target_manifest_sha256",
    "state",
    "governed_receipt_sha256",
    "lease_token",
)
READ_SOURCE_ERASURE_TARGET_FIELDS = (
    "owner_user_id",
    "message_id",
    "thread_id",
    "source_created_at",
    "target_sha256",
)
FINALIZE_CONVERSATION_SOURCE_ERASURE_FIELDS = (
    "outcome",
    "receipt_sha256",
    "deleted_message_count",
    "deleted_thread_count",
    "deleted_attachment_count",
    "deleted_bridge_row_count",
    "completed_at",
)

REGISTER_SOURCE_ERASURE_FIELDS = (
    "outcome",
    "state",
    "received_target_count",
)
APPEND_SOURCE_ERASURE_TARGETS_FIELDS = (
    "outcome",
    "inserted_count",
    "received_target_count",
)
SEAL_SOURCE_ERASURE_FIELDS = (
    "outcome",
    "state",
    "touched_claim_count",
)
PREPARE_SOURCE_ERASURE_CLAIM_DELETIONS_FIELDS = (
    "outcome",
    "prepared_count",
    "remaining_count",
)
READ_SUCCESSOR_SOURCE_ERASURE_PROGRESS_FIELDS = (
    "owner_user_id",
    "operation_id",
    "state",
    "target_count",
    "target_manifest_sha256",
    "touched_claim_count",
    "prepared_claim_count",
    "deleted_claim_count",
    "governed_receipt_sha256",
    "conversation_receipt_sha256",
    "last_error_code",
)
FINALIZE_SOURCE_ERASURE_MEMORY_FIELDS = (
    "outcome",
    "governed_receipt_sha256",
    "target_count",
    "target_manifest_sha256",
    "touched_claim_count",
    "deleted_claim_count",
)
ACK_SOURCE_ERASURE_CONVERSATION_DELETED_FIELDS = (
    "outcome",
    "receipt_sha256",
    "completed_at",
)
READ_SUCCESSOR_SOURCE_ERASURE_RECEIPT_FIELDS = (
    "operation_id",
    "selector_sha256",
    "target_manifest_sha256",
    "target_count",
    "touched_claim_count",
    "claim_deletion_receipt_count",
    "pre_fence_provider_dispatch_count",
    "governed_absence_sha256",
    "conversation_receipt_sha256",
    "receipt_sha256",
    "completed_at",
)

_TARGET_PAGE_SIZE = 500
_LEGACY_PROJECT_THREAD_CATALOG_SQLSTATE = "P0001"
_LEGACY_PROJECT_THREAD_CATALOG_MESSAGES = frozenset(
    {
        (
            "unclassified inbound chat deletion dependency: "
            "memory.project_thread_binding_event -> public.threads"
        ),
        (
            "unclassified inbound chat deletion dependency: "
            "memory.project_thread_component_binding_event_v5 -> "
            "public.threads"
        ),
    }
)


def _conversation_database_failure(
    error: Exception,
) -> DeletionRepositoryFailure:
    """Translate only the two sealed legacy project catalog conflicts."""

    sqlstate = getattr(error, "sqlstate", None)
    message = getattr(error, "message", None)
    if (
        type(sqlstate) is str
        and sqlstate == _LEGACY_PROJECT_THREAD_CATALOG_SQLSTATE
        and type(message) is str
        and message in _LEGACY_PROJECT_THREAD_CATALOG_MESSAGES
    ):
        return DeletionRepositoryFailure.LEGACY_PROJECT_THREAD_DEPENDENCY
    return DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE


def _uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc
    return require_uuid(parsed, code)


def _row(
    value: Mapping[str, object] | None,
    fields: tuple[str, ...],
    code: str,
) -> dict[str, object]:
    row = dict(value) if value is not None else {}
    if tuple(row) != fields:
        raise ContractViolation(code)
    return row


def _enum(enum_type: Any, value: object, code: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _mutation_outcome(value: object, allowed: set[str]) -> DeletionMutationOutcome:
    if not isinstance(value, str) or value not in allowed:
        raise ContractViolation("invalid_source_erasure_mutation_outcome")
    if value == "replayed":
        return DeletionMutationOutcome.REPLAYED
    return DeletionMutationOutcome.APPLIED


def _material(domain: str, row: Mapping[str, object]) -> str:
    return canonical_sha256(domain, dict(row))


class PostgresConversationDeletionRepository:
    def __init__(self, connection: Any) -> None:
        if connection is None:
            raise ContractViolation("conversation_deletion_connection_required")
        self._connection = connection

    @asynccontextmanager
    async def _owner_transaction(
        self,
        command: BoundConversationDeletion,
    ) -> AsyncIterator[Any]:
        """Bind and verify owner authority inside the deletion transaction."""

        if not isinstance(command, BoundConversationDeletion):
            raise ContractViolation("invalid_deletion_command")
        authority = command.authority
        async with self._connection.transaction():
            await self._connection.execute(
                _SET_OWNER_CONTEXT_SQL,
                str(authority.owner_user_id),
            )
            await self._connection.execute(
                _SET_AUTH_CONTEXT_SQL,
                authority.authentication_manifest_sha256,
            )
            bound_owner = await self._connection.fetchval(
                _READ_OWNER_CONTEXT_SQL
            )
            bound_auth_context = await self._connection.fetchval(
                _READ_AUTH_CONTEXT_SQL
            )
            if bound_owner != str(authority.owner_user_id):
                raise ContractViolation(
                    "conversation_deletion_owner_context_mismatch"
                )
            if (
                bound_auth_context
                != authority.authentication_manifest_sha256
            ):
                raise ContractViolation(
                    "conversation_deletion_auth_context_mismatch"
                )
            yield self._connection

    def _status(
        self,
        row: Mapping[str, object],
        *,
        owner_user_id: UUID,
    ) -> ConversationErasureStatus:
        return ConversationErasureStatus(
            owner_user_id=owner_user_id,
            operation_id=_uuid(
                row["operation_id"], "invalid_conversation_erasure_operation"
            ),
            selector_kind=_enum(
                DeletionSelectorKind,
                row["selector_kind"],
                "invalid_conversation_erasure_selector_kind",
            ),
            state=_enum(
                ConversationErasureState,
                row["state"],
                "invalid_conversation_erasure_state",
            ),
            target_count=row["target_count"],
            selector_sha256=row["selector_sha256"],
            target_manifest_sha256=row["target_manifest_sha256"],
            governed_receipt_sha256=row["governed_receipt_sha256"],
            last_error_code=row["last_error_code"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    async def request_erasure(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus:
        if not isinstance(command, BoundConversationDeletion):
            raise ContractViolation("invalid_deletion_command")
        request = command.request
        try:
            async with self._owner_transaction(command) as connection:
                begin = _row(
                    await connection.fetchrow(
                        _BEGIN_SOURCE_ERASURE_SQL,
                        request.operation_id,
                        request.selector_kind.value,
                        request.thread_id,
                        request.anchor_message_id,
                        request.recent_window_seconds,
                        request.confirmation_sha256,
                    ),
                    BEGIN_SOURCE_ERASURE_FIELDS,
                    "invalid_begin_source_erasure_receipt",
                )
                if begin["outcome"] not in {"fenced", "replayed"}:
                    raise ContractViolation(
                        "invalid_begin_source_erasure_outcome"
                    )
                if _uuid(
                    begin["operation_id"], "invalid_deletion_operation"
                ) != command.operation_id:
                    raise ContractViolation(
                        "source_erasure_operation_mismatch"
                    )
                status = await self._read_erasure_status(
                    connection,
                    command,
                )
                if status is None or any(
                    (
                        status.state.value != begin["state"],
                        status.target_count != begin["target_count"],
                        status.selector_sha256 != begin["selector_sha256"],
                        status.target_manifest_sha256
                        != begin["target_manifest_sha256"],
                    )
                ):
                    raise ContractViolation(
                        "begin_source_erasure_receipt_mismatch"
                    )
                return status
        except ContractViolation:
            raise
        except Exception as error:
            raise DeletionRepositoryError(
                _conversation_database_failure(error)
            ) from None

    async def read_erasure_status(
        self,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus | None:
        if not isinstance(command, BoundConversationDeletion):
            raise ContractViolation("invalid_deletion_command")
        try:
            async with self._owner_transaction(command) as connection:
                return await self._read_erasure_status(connection, command)
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def _read_erasure_status(
        self,
        connection: Any,
        command: BoundConversationDeletion,
    ) -> ConversationErasureStatus | None:
        value = await connection.fetchrow(
            _READ_SOURCE_ERASURE_SQL,
            command.operation_id,
        )
        if value is None:
            return None
        status = self._status(
            _row(
                value,
                READ_SOURCE_ERASURE_FIELDS,
                "invalid_read_source_erasure_receipt",
            ),
            owner_user_id=command.authority.owner_user_id,
        )
        return require_request_status_binding(command, status)

    async def lease_erasure(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> ConversationErasureLease | None:
        if worker_id != "governed-memory-deletion-coordinator-1":
            raise ContractViolation("deletion_coordinator_worker_id_mismatch")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ContractViolation("invalid_deletion_coordinator_lease")
        try:
            value = await self._connection.fetchrow(
                _LEASE_SOURCE_ERASURE_SQL,
                worker_id,
                lease_seconds,
            )
            if value is None:
                return None
            row = _row(
                value,
                LEASE_SOURCE_ERASURE_FIELDS,
                "invalid_lease_source_erasure_receipt",
            )
            return ConversationErasureLease(
                operation_id=_uuid(
                    row["operation_id"], "invalid_erasure_lease_operation"
                ),
                owner_user_id=_uuid(
                    row["owner_user_id"], "invalid_erasure_lease_owner"
                ),
                selector_kind=_enum(
                    DeletionSelectorKind,
                    row["selector_kind"],
                    "invalid_erasure_lease_selector",
                ),
                selector_sha256=row["selector_sha256"],
                target_count=row["target_count"],
                target_manifest_sha256=row["target_manifest_sha256"],
                state=_enum(
                    ConversationErasureState,
                    row["state"],
                    "invalid_erasure_lease_state",
                ),
                governed_receipt_sha256=row["governed_receipt_sha256"],
                lease_token=_uuid(
                    row["lease_token"], "invalid_erasure_lease_token"
                ),
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def read_erasure_targets(
        self,
        lease: ConversationErasureLease,
    ) -> tuple[ConversationErasureTarget, ...]:
        if not isinstance(lease, ConversationErasureLease):
            raise ContractViolation("invalid_conversation_erasure_lease")
        targets: list[ConversationErasureTarget] = []
        cursor_created_at: datetime | None = None
        cursor_message_id: UUID | None = None
        try:
            while True:
                values = await self._connection.fetch(
                    _READ_SOURCE_ERASURE_TARGETS_SQL,
                    lease.operation_id,
                    lease.lease_token,
                    cursor_created_at,
                    cursor_message_id,
                    _TARGET_PAGE_SIZE,
                )
                rows = [
                    _row(
                        value,
                        READ_SOURCE_ERASURE_TARGET_FIELDS,
                        "invalid_source_erasure_target_row",
                    )
                    for value in values
                ]
                for row in rows:
                    target = ConversationErasureTarget(
                        owner_user_id=_uuid(
                            row["owner_user_id"],
                            "invalid_erasure_target_owner",
                        ),
                        operation_id=lease.operation_id,
                        message_id=_uuid(
                            row["message_id"],
                            "invalid_erasure_target_message",
                        ),
                        thread_id=_uuid(
                            row["thread_id"],
                            "invalid_erasure_target_thread",
                        ),
                        source_created_at=row["source_created_at"],
                        target_sha256=row["target_sha256"],
                    )
                    targets.append(target)
                if len(targets) > lease.target_count:
                    raise ContractViolation("erasure_target_count_overflow")
                if len(rows) < _TARGET_PAGE_SIZE:
                    break
                last = targets[-1]
                cursor_created_at = require_utc(last.source_created_at)
                cursor_message_id = last.message_id
            return tuple(targets)
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def release_erasure_lease(
        self,
        lease: ConversationErasureLease,
    ) -> None:
        try:
            outcome = await self._connection.fetchval(
                _RELEASE_SOURCE_ERASURE_LEASE_SQL,
                lease.operation_id,
                lease.lease_token,
            )
            if outcome not in {"released", "replayed"}:
                raise ContractViolation("invalid_erasure_lease_release")
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def mark_successor_memory_deleted(
        self,
        lease: ConversationErasureLease,
        *,
        governed_receipt_sha256: str,
    ) -> None:
        receipt = require_sha256(
            governed_receipt_sha256,
            "invalid_governed_erasure_receipt",
        )
        try:
            outcome = await self._connection.fetchval(
                _MARK_SOURCE_ERASURE_GOVERNED_DELETED_SQL,
                lease.operation_id,
                lease.lease_token,
                receipt,
                lease.target_count,
                lease.target_manifest_sha256,
            )
            if outcome not in {"governed_deleted", "replayed"}:
                raise ContractViolation(
                    "invalid_mark_governed_deleted_outcome"
                )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def finalize_conversation_erasure(
        self,
        lease: ConversationErasureLease,
    ) -> ConversationFinalizationReceipt:
        governed_receipt = require_sha256(
            lease.governed_receipt_sha256,
            "invalid_erasure_lease_governed_receipt",
        )
        try:
            row = _row(
                await self._connection.fetchrow(
                    _FINALIZE_CONVERSATION_SOURCE_ERASURE_SQL,
                    lease.operation_id,
                    lease.lease_token,
                    governed_receipt,
                ),
                FINALIZE_CONVERSATION_SOURCE_ERASURE_FIELDS,
                "invalid_finalize_conversation_erasure_receipt",
            )
            return ConversationFinalizationReceipt(
                owner_user_id=lease.owner_user_id,
                operation_id=lease.operation_id,
                outcome=_mutation_outcome(
                    row["outcome"],
                    {"conversation_deleted_pending_ack", "replayed"},
                ),
                receipt_sha256=row["receipt_sha256"],
                deleted_message_count=row["deleted_message_count"],
                deleted_thread_count=row["deleted_thread_count"],
                deleted_attachment_count=row["deleted_attachment_count"],
                deleted_bridge_row_count=row["deleted_bridge_row_count"],
                completed_at=row["completed_at"],
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def acknowledge_erasure_completion(
        self,
        lease: ConversationErasureLease,
        *,
        conversation_receipt_sha256: str,
    ) -> None:
        receipt = require_sha256(
            conversation_receipt_sha256,
            "invalid_conversation_erasure_receipt",
        )
        try:
            outcome = await self._connection.fetchval(
                _ACK_CONVERSATION_SOURCE_ERASURE_COMPLETION_SQL,
                lease.operation_id,
                lease.lease_token,
                receipt,
            )
            if outcome not in {"completed", "replayed"}:
                raise ContractViolation(
                    "invalid_ack_erasure_completion_outcome"
                )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None

    async def fail_erasure(
        self,
        lease: ConversationErasureLease,
        *,
        failure: DeletionRepositoryFailure,
        retry_after_seconds: int,
    ) -> ConversationErasureState:
        if not isinstance(failure, DeletionRepositoryFailure):
            raise ContractViolation("invalid_deletion_repository_failure")
        if retry_after_seconds not in {0, 30}:
            raise ContractViolation("invalid_deletion_retry_policy")
        failure_mode = (
            "manual_review" if retry_after_seconds == 0 else "retryable"
        )
        try:
            outcome = await self._connection.fetchval(
                _FAIL_SOURCE_ERASURE_SQL,
                lease.operation_id,
                lease.lease_token,
                failure_mode,
                failure.value,
            )
            if outcome not in {
                failure_mode,
                "conversation_deleted_pending_ack",
                "replayed",
            }:
                raise ContractViolation("invalid_fail_source_erasure_outcome")
            if outcome == "replayed":
                return lease.state
            return _enum(
                ConversationErasureState,
                outcome,
                "invalid_fail_source_erasure_state",
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.CONVERSATION_UNAVAILABLE
            ) from None


class PostgresSuccessorDeletionRepository:
    def __init__(self, connection: Any) -> None:
        if connection is None:
            raise ContractViolation("successor_deletion_connection_required")
        self._connection = connection

    @staticmethod
    def _receipt(
        lease: ConversationErasureLease,
        *,
        row: Mapping[str, object],
        domain: str,
        allowed_outcomes: set[str],
        material_sha256: str | None = None,
    ) -> DeletionMutationReceipt:
        return DeletionMutationReceipt(
            owner_user_id=lease.owner_user_id,
            operation_id=lease.operation_id,
            outcome=_mutation_outcome(row["outcome"], allowed_outcomes),
            material_sha256=(
                _material(domain, row)
                if material_sha256 is None
                else require_sha256(material_sha256)
            ),
        )

    async def stage_source_erasure(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt:
        try:
            row = _row(
                await self._connection.fetchrow(
                    _REGISTER_SOURCE_ERASURE_SQL,
                    lease.operation_id,
                    lease.owner_user_id,
                    lease.selector_kind.value,
                    lease.selector_sha256,
                    lease.target_count,
                    lease.target_manifest_sha256,
                ),
                REGISTER_SOURCE_ERASURE_FIELDS,
                "invalid_register_source_erasure_receipt",
            )
            if row["state"] not in {
                "receiving",
                "fenced",
                "claim_deletion_pending",
                "memory_deleted",
                "completed",
                "manual_review",
            }:
                raise ContractViolation("invalid_register_source_erasure_state")
            require_exact_int(
                row["received_target_count"],
                code="invalid_register_source_erasure_received_count",
                maximum=lease.target_count,
            )
            return self._receipt(
                lease,
                row=row,
                domain="governed_memory.register_source_erasure_receipt.v1",
                allowed_outcomes={"registered", "replayed"},
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def stage_source_erasure_targets(
        self,
        lease: ConversationErasureLease,
        targets: tuple[ConversationErasureTarget, ...],
    ) -> DeletionMutationReceipt:
        validate_erasure_targets(lease, targets)
        try:
            if not targets:
                return DeletionMutationReceipt(
                    owner_user_id=lease.owner_user_id,
                    operation_id=lease.operation_id,
                    outcome=DeletionMutationOutcome.APPLIED,
                    material_sha256=canonical_sha256(
                        "governed_memory.append_source_erasure_targets_receipt.v1",
                        (),
                    ),
                )
            page_material: list[str] = []
            applied = False
            received_count = 0
            for offset in range(0, len(targets), _TARGET_PAGE_SIZE):
                page = targets[offset : offset + _TARGET_PAGE_SIZE]
                row = _row(
                    await self._connection.fetchrow(
                        _APPEND_SOURCE_ERASURE_TARGETS_SQL,
                        lease.operation_id,
                        [target.message_id for target in page],
                        [target.thread_id for target in page],
                        [target.source_created_at for target in page],
                        [target.target_sha256 for target in page],
                    ),
                    APPEND_SOURCE_ERASURE_TARGETS_FIELDS,
                    "invalid_append_source_erasure_targets_receipt",
                )
                outcome = _mutation_outcome(
                    row["outcome"], {"appended", "replayed"}
                )
                applied = applied or outcome is DeletionMutationOutcome.APPLIED
                received_count = row["received_target_count"]
                page_material.append(
                    _material(
                        "governed_memory.append_source_erasure_target_page.v1",
                        row,
                    )
                )
            if received_count != lease.target_count:
                raise ContractViolation(
                    "append_source_erasure_target_count_mismatch"
                )
            return DeletionMutationReceipt(
                owner_user_id=lease.owner_user_id,
                operation_id=lease.operation_id,
                outcome=(
                    DeletionMutationOutcome.APPLIED
                    if applied
                    else DeletionMutationOutcome.REPLAYED
                ),
                material_sha256=canonical_sha256(
                    "governed_memory.append_source_erasure_targets_receipt.v1",
                    tuple(page_material),
                ),
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def seal_source_erasure_targets(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt:
        try:
            row = _row(
                await self._connection.fetchrow(
                    _SEAL_SOURCE_ERASURE_SQL,
                    lease.operation_id,
                ),
                SEAL_SOURCE_ERASURE_FIELDS,
                "invalid_seal_source_erasure_receipt",
            )
            if row["state"] not in {
                "fenced",
                "claim_deletion_pending",
                "memory_deleted",
                "manual_review",
            }:
                raise ContractViolation("invalid_seal_source_erasure_state")
            return self._receipt(
                lease,
                row=row,
                domain="governed_memory.seal_source_erasure_receipt.v1",
                allowed_outcomes={"sealed", "manual_review", "replayed"},
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def request_next_source_erasure_claim_deletion(
        self,
        lease: ConversationErasureLease,
    ) -> ClaimDeletionStepReceipt:
        try:
            row = _row(
                await self._connection.fetchrow(
                    _PREPARE_SOURCE_ERASURE_CLAIM_DELETIONS_SQL,
                    lease.operation_id,
                    1,
                ),
                PREPARE_SOURCE_ERASURE_CLAIM_DELETIONS_FIELDS,
                "invalid_prepare_claim_deletions_receipt",
            )
            outcome_text = row["outcome"]
            if outcome_text == "prepared":
                outcome = ClaimDeletionStepOutcome.PREPARED
            elif outcome_text == "no_new_claim_deletions":
                outcome = (
                    ClaimDeletionStepOutcome.NO_NEW_CLAIM_DELETIONS
                )
            else:
                raise ContractViolation("invalid_prepare_claim_deletion_outcome")
            # The SQL batch receipt is intentionally claim-id-free.
            return ClaimDeletionStepReceipt(
                owner_user_id=lease.owner_user_id,
                operation_id=lease.operation_id,
                outcome=outcome,
                material_sha256=_material(
                    "governed_memory.prepare_claim_deletions_receipt.v1", row
                ),
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    def _progress(self, row: Mapping[str, object]) -> SuccessorErasureProgress:
        return SuccessorErasureProgress(
            owner_user_id=_uuid(
                row["owner_user_id"], "invalid_successor_progress_owner"
            ),
            operation_id=_uuid(
                row["operation_id"], "invalid_successor_progress_operation"
            ),
            state=_enum(
                SuccessorErasureState,
                row["state"],
                "invalid_successor_erasure_state",
            ),
            target_count=row["target_count"],
            target_manifest_sha256=row["target_manifest_sha256"],
            touched_claim_count=row["touched_claim_count"],
            prepared_claim_count=row["prepared_claim_count"],
            deleted_claim_count=row["deleted_claim_count"],
            governed_receipt_sha256=row["governed_receipt_sha256"],
            conversation_receipt_sha256=row["conversation_receipt_sha256"],
            last_error_code=row["last_error_code"],
        )

    async def read_source_erasure_progress(
        self,
        lease: ConversationErasureLease,
    ) -> SuccessorErasureProgress:
        try:
            return self._progress(
                _row(
                    await self._connection.fetchrow(
                        _READ_SUCCESSOR_SOURCE_ERASURE_PROGRESS_SQL,
                        lease.operation_id,
                    ),
                    READ_SUCCESSOR_SOURCE_ERASURE_PROGRESS_FIELDS,
                    "invalid_source_erasure_progress_receipt",
                )
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def finalize_source_erasure_memory(
        self,
        lease: ConversationErasureLease,
    ) -> DeletionMutationReceipt:
        try:
            row = _row(
                await self._connection.fetchrow(
                    _FINALIZE_SOURCE_ERASURE_MEMORY_SQL,
                    lease.operation_id,
                ),
                FINALIZE_SOURCE_ERASURE_MEMORY_FIELDS,
                "invalid_finalize_source_erasure_memory_receipt",
            )
            if (
                row["target_count"] != lease.target_count
                or row["target_manifest_sha256"]
                != lease.target_manifest_sha256
                or row["touched_claim_count"] != row["deleted_claim_count"]
            ):
                raise ContractViolation(
                    "finalize_source_erasure_memory_receipt_mismatch"
                )
            return self._receipt(
                lease,
                row=row,
                domain="governed_memory.finalize_source_erasure_memory.v1",
                allowed_outcomes={"memory_deleted", "replayed"},
                material_sha256=row["governed_receipt_sha256"],
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def acknowledge_conversation_deleted(
        self,
        lease: ConversationErasureLease,
        *,
        conversation_receipt_sha256: str,
    ) -> DeletionMutationReceipt:
        receipt = require_sha256(
            conversation_receipt_sha256,
            "invalid_conversation_erasure_receipt",
        )
        try:
            row = _row(
                await self._connection.fetchrow(
                    _ACK_SOURCE_ERASURE_CONVERSATION_DELETED_SQL,
                    lease.operation_id,
                    receipt,
                ),
                ACK_SOURCE_ERASURE_CONVERSATION_DELETED_FIELDS,
                "invalid_ack_conversation_deleted_receipt",
            )
            require_utc(
                row["completed_at"], "invalid_source_erasure_completed_at"
            )
            return self._receipt(
                lease,
                row=row,
                domain="governed_memory.ack_conversation_deleted.v1",
                allowed_outcomes={"completed", "replayed"},
                material_sha256=row["receipt_sha256"],
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None

    async def read_source_erasure_receipt(
        self,
        lease: ConversationErasureLease,
    ) -> SourceErasureReceipt | None:
        try:
            value = await self._connection.fetchrow(
                _READ_SUCCESSOR_SOURCE_ERASURE_RECEIPT_SQL,
                lease.operation_id,
            )
            if value is None:
                return None
            row = _row(
                value,
                READ_SUCCESSOR_SOURCE_ERASURE_RECEIPT_FIELDS,
                "invalid_read_source_erasure_receipt",
            )
            return SourceErasureReceipt(
                operation_id=_uuid(
                    row["operation_id"], "invalid_source_erasure_operation"
                ),
                selector_sha256=row["selector_sha256"],
                target_manifest_sha256=row["target_manifest_sha256"],
                target_count=row["target_count"],
                touched_claim_count=row["touched_claim_count"],
                claim_deletion_receipt_count=(
                    row["claim_deletion_receipt_count"]
                ),
                pre_fence_provider_dispatch_count=(
                    row["pre_fence_provider_dispatch_count"]
                ),
                governed_absence_sha256=row["governed_absence_sha256"],
                conversation_receipt_sha256=(
                    row["conversation_receipt_sha256"]
                ),
                receipt_sha256=row["receipt_sha256"],
                completed_at=row["completed_at"],
            )
        except ContractViolation:
            raise
        except Exception:
            raise DeletionRepositoryError(
                DeletionRepositoryFailure.SUCCESSOR_UNAVAILABLE
            ) from None


__all__ = [
    "ACK_SOURCE_ERASURE_CONVERSATION_DELETED_FIELDS",
    "APPEND_SOURCE_ERASURE_TARGETS_FIELDS",
    "BEGIN_SOURCE_ERASURE_FIELDS",
    "FINALIZE_CONVERSATION_SOURCE_ERASURE_FIELDS",
    "FINALIZE_SOURCE_ERASURE_MEMORY_FIELDS",
    "LEASE_SOURCE_ERASURE_FIELDS",
    "PostgresConversationDeletionRepository",
    "PostgresSuccessorDeletionRepository",
    "PREPARE_SOURCE_ERASURE_CLAIM_DELETIONS_FIELDS",
    "READ_SOURCE_ERASURE_FIELDS",
    "READ_SOURCE_ERASURE_TARGET_FIELDS",
    "READ_SUCCESSOR_SOURCE_ERASURE_PROGRESS_FIELDS",
    "READ_SUCCESSOR_SOURCE_ERASURE_RECEIPT_FIELDS",
    "REGISTER_SOURCE_ERASURE_FIELDS",
    "SEAL_SOURCE_ERASURE_FIELDS",
]
