from __future__ import annotations

"""Closed persistence ports; implementations map each method to one stored procedure."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from .contracts import (
    ContractViolation,
    EligibilityDecision,
    OperationOutcome,
    require_exact_int,
    require_key,
    require_sha256,
    require_uuid,
)
from .admission import ReviewDecision
from .extraction import validate_proposal_item
from .worker import ingest_successor_receipt_sha256


class RepositoryWriteKind(str, Enum):
    INGEST_DECISION = "ingest_decision"
    PROVIDER_DISPATCH = "provider_dispatch"
    PROVIDER_COMPLETION = "provider_completion"
    PROVIDER_FAILURE = "provider_failure"
    PROPOSAL_REVIEW = "proposal_review"
    LIFECYCLE_TRANSITION = "lifecycle_transition"
    PROJECTION_COMPLETION = "projection_completion"
    ANSWER_BINDING = "answer_binding"
    BRIDGE_FAILURE = "bridge_failure"


class ProviderFailureDisposition(str, Enum):
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ProviderFailureReason(str, Enum):
    ADAPTER_REJECTED_BEFORE_SEND = "adapter_rejected_before_send"
    CONNECTION_FAILED_BEFORE_SEND = "connection_failed_before_send"
    LOCAL_SERIALIZATION_FAILED_BEFORE_SEND = "local_serialization_failed_before_send"
    PROVIDER_PROVED_NOT_ACCEPTED = "provider_proved_not_accepted"
    LEASE_EXPIRED_BEFORE_DISPATCH = "lease_expired_before_dispatch"
    INVALID_PROVIDER_OUTPUT = "invalid_provider_output"
    PROVIDER_AUTH_REJECTED = "provider_auth_rejected"
    PROVIDER_REQUEST_REJECTED = "provider_request_rejected"
    PROVIDER_USAGE_CONTRACT_VIOLATION = "provider_usage_contract_violation"
    RESPONSE_SCHEMA_VIOLATION = "response_schema_violation"
    CONNECTION_RESET_AFTER_DISPATCH = "connection_reset_after_dispatch"
    DISPATCH_CRASH = "dispatch_crash"
    PROVIDER_TIMEOUT_AFTER_DISPATCH = "provider_timeout_after_dispatch"
    RESPONSE_PERSISTENCE_FAILED_AFTER_DISPATCH = "response_persistence_failed_after_dispatch"
    LEASE_EXPIRED_AFTER_DISPATCH = "lease_expired_after_dispatch"


class BridgeFailureMode(str, Enum):
    RETRYABLE = "retryable"
    FAILED_TERMINAL = "failed_terminal"


class BridgeFailureReason(str, Enum):
    CONVERSATION_READ_FAILED = "conversation_read_failed"
    SUCCESSOR_WRITE_FAILED = "successor_write_failed"
    WORKER_TRANSIENT_FAILURE = "worker_transient_failure"
    BRIDGE_CONTRACT_VIOLATION = "bridge_contract_violation"
    ELIGIBILITY_CONTRACT_VIOLATION = "eligibility_contract_violation"
    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    SUCCESSOR_RECEIPT_MISMATCH = "successor_receipt_mismatch"


class ProjectionCompletionOutcome(str, Enum):
    APPLIED = "applied"
    RETRYABLE = "retryable"
    FAILED_TERMINAL = "failed_terminal"


class ProjectionFailureReason(str, Enum):
    EMBEDDING_PROVIDER_UNAVAILABLE = "embedding_provider_unavailable"
    EMBEDDING_TIMEOUT = "embedding_timeout"
    QDRANT_RATE_LIMITED = "qdrant_rate_limited"
    QDRANT_TIMEOUT = "qdrant_timeout"
    QDRANT_UNAVAILABLE = "qdrant_unavailable"
    QDRANT_VERIFICATION_INCONCLUSIVE = "qdrant_verification_inconclusive"
    EMBEDDING_CONTRACT_VIOLATION = "embedding_contract_violation"
    PROJECTION_CONTRACT_VIOLATION = "projection_contract_violation"
    QDRANT_COLLECTION_MISMATCH = "qdrant_collection_mismatch"
    QDRANT_DIMENSION_MISMATCH = "qdrant_dimension_mismatch"
    QDRANT_RECEIPT_CONTRACT_VIOLATION = "qdrant_receipt_contract_violation"


class AnswerBindingOutcome(str, Enum):
    EXPOSED = "exposed"
    NO_MEMORY_SELECTED = "no_memory_selected"


_BRIDGE_RETRYABLE_REASONS = frozenset(
    {
        BridgeFailureReason.CONVERSATION_READ_FAILED,
        BridgeFailureReason.SUCCESSOR_WRITE_FAILED,
        BridgeFailureReason.WORKER_TRANSIENT_FAILURE,
    }
)
_BRIDGE_TERMINAL_REASONS = frozenset(
    {
        BridgeFailureReason.BRIDGE_CONTRACT_VIOLATION,
        BridgeFailureReason.ELIGIBILITY_CONTRACT_VIOLATION,
        BridgeFailureReason.SOURCE_BINDING_MISMATCH,
        BridgeFailureReason.SUCCESSOR_RECEIPT_MISMATCH,
    }
)
_PROVIDER_RETRYABLE_REASONS = frozenset(
    {
        ProviderFailureReason.ADAPTER_REJECTED_BEFORE_SEND,
        ProviderFailureReason.CONNECTION_FAILED_BEFORE_SEND,
        ProviderFailureReason.LOCAL_SERIALIZATION_FAILED_BEFORE_SEND,
        ProviderFailureReason.PROVIDER_PROVED_NOT_ACCEPTED,
        ProviderFailureReason.LEASE_EXPIRED_BEFORE_DISPATCH,
    }
)
_PROVIDER_TERMINAL_REASONS = frozenset(
    {
        ProviderFailureReason.INVALID_PROVIDER_OUTPUT,
        ProviderFailureReason.PROVIDER_AUTH_REJECTED,
        ProviderFailureReason.PROVIDER_REQUEST_REJECTED,
        ProviderFailureReason.PROVIDER_USAGE_CONTRACT_VIOLATION,
        ProviderFailureReason.RESPONSE_SCHEMA_VIOLATION,
    }
)
_PROVIDER_UNKNOWN_REASONS = frozenset(
    {
        ProviderFailureReason.CONNECTION_RESET_AFTER_DISPATCH,
        ProviderFailureReason.DISPATCH_CRASH,
        ProviderFailureReason.PROVIDER_TIMEOUT_AFTER_DISPATCH,
        ProviderFailureReason.RESPONSE_PERSISTENCE_FAILED_AFTER_DISPATCH,
        ProviderFailureReason.LEASE_EXPIRED_AFTER_DISPATCH,
    }
)
_PROJECTION_RETRYABLE_REASONS = frozenset(
    {
        ProjectionFailureReason.EMBEDDING_PROVIDER_UNAVAILABLE,
        ProjectionFailureReason.EMBEDDING_TIMEOUT,
        ProjectionFailureReason.QDRANT_RATE_LIMITED,
        ProjectionFailureReason.QDRANT_TIMEOUT,
        ProjectionFailureReason.QDRANT_UNAVAILABLE,
        ProjectionFailureReason.QDRANT_VERIFICATION_INCONCLUSIVE,
    }
)
_PROJECTION_TERMINAL_REASONS = frozenset(
    {
        ProjectionFailureReason.EMBEDDING_CONTRACT_VIOLATION,
        ProjectionFailureReason.PROJECTION_CONTRACT_VIOLATION,
        ProjectionFailureReason.QDRANT_COLLECTION_MISMATCH,
        ProjectionFailureReason.QDRANT_DIMENSION_MISMATCH,
        ProjectionFailureReason.QDRANT_RECEIPT_CONTRACT_VIOLATION,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeFailure:
    mode: BridgeFailureMode
    reason: BridgeFailureReason
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.mode, BridgeFailureMode) or not isinstance(
            self.reason, BridgeFailureReason
        ):
            raise ContractViolation("invalid_bridge_failure")
        retry_after = require_exact_int(
            self.retry_after_seconds,
            code="invalid_bridge_retry_after_seconds",
            maximum=3_600,
        )
        if self.mode is BridgeFailureMode.RETRYABLE:
            if self.reason not in _BRIDGE_RETRYABLE_REASONS:
                raise ContractViolation("invalid_retryable_bridge_failure")
        elif self.reason not in _BRIDGE_TERMINAL_REASONS or retry_after != 0:
            raise ContractViolation("invalid_terminal_bridge_failure")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFailure:
    disposition: ProviderFailureDisposition
    reason: ProviderFailureReason
    request_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ProviderFailureDisposition) or not isinstance(
            self.reason, ProviderFailureReason
        ):
            raise ContractViolation("invalid_provider_failure")
        if self.request_sha256 is not None:
            require_sha256(self.request_sha256, "invalid_provider_failure_request_sha256")
        expected = {
            ProviderFailureDisposition.RETRYABLE_FAILURE: _PROVIDER_RETRYABLE_REASONS,
            ProviderFailureDisposition.TERMINAL_FAILURE: _PROVIDER_TERMINAL_REASONS,
            ProviderFailureDisposition.OUTCOME_UNKNOWN: _PROVIDER_UNKNOWN_REASONS,
        }[self.disposition]
        if self.reason not in expected:
            raise ContractViolation("provider_failure_disposition_mismatch")
        if (
            self.disposition is ProviderFailureDisposition.RETRYABLE_FAILURE
            and self.request_sha256 is not None
        ) or (
            self.disposition in {
                ProviderFailureDisposition.TERMINAL_FAILURE,
                ProviderFailureDisposition.OUTCOME_UNKNOWN,
            }
            and self.request_sha256 is None
        ):
            raise ContractViolation("provider_failure_request_shape_mismatch")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionCompletion:
    outcome: ProjectionCompletionOutcome
    physical_collection_name: str | None
    vector_sha256: str | None
    verification_sha256: str | None
    verification_receipt_sha256: str | None
    error_code: ProjectionFailureReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProjectionCompletionOutcome):
            raise ContractViolation("invalid_projection_completion_outcome")
        for field, value in (
            ("vector", self.vector_sha256),
            ("verification", self.verification_sha256),
            ("verification_receipt", self.verification_receipt_sha256),
        ):
            if value is not None:
                require_sha256(value, f"invalid_projection_{field}_sha256")
        if self.outcome is ProjectionCompletionOutcome.APPLIED:
            if (
                self.error_code is not None
                or self.physical_collection_name is None
                or (self.vector_sha256 is None)
                == (self.verification_sha256 is None)
                or (self.verification_sha256 is None)
                != (self.verification_receipt_sha256 is None)
            ):
                raise ContractViolation("invalid_applied_projection_completion")
            return
        if self.error_code is None or any(
            value is not None
            for value in (
                self.physical_collection_name,
                self.vector_sha256,
                self.verification_sha256,
                self.verification_receipt_sha256,
            )
        ):
            raise ContractViolation("invalid_failed_projection_completion")
        expected = (
            _PROJECTION_RETRYABLE_REASONS
            if self.outcome is ProjectionCompletionOutcome.RETRYABLE
            else _PROJECTION_TERMINAL_REASONS
        )
        if self.error_code not in expected:
            raise ContractViolation("projection_failure_outcome_mismatch")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteExtractionProposal:
    epistemic_state: str
    fact_index: int
    object_display_name: str | None
    object_entity_key: str | None
    object_entity_type: str | None
    object_kind: str
    object_literal: str | None
    operation_id: UUID
    predicate: str
    proposal_id: UUID
    proposal_sha256: str
    semantic_key_sha256: str
    sensitivity: str
    subject_display_name: str | None
    subject_entity_key: str
    subject_entity_type: str

    def __post_init__(self) -> None:
        require_uuid(self.operation_id, "invalid_complete_proposal_operation")
        require_uuid(self.proposal_id, "invalid_complete_proposal_id")
        validate_proposal_item(self.material)

    @property
    def material(self) -> dict[str, object]:
        return {
            "epistemic_state": self.epistemic_state,
            "fact_index": self.fact_index,
            "object_display_name": self.object_display_name,
            "object_entity_key": self.object_entity_key,
            "object_entity_type": self.object_entity_type,
            "object_kind": self.object_kind,
            "object_literal": self.object_literal,
            "operation_id": str(self.operation_id),
            "predicate": self.predicate,
            "proposal_id": str(self.proposal_id),
            "proposal_sha256": self.proposal_sha256,
            "semantic_key_sha256": self.semantic_key_sha256,
            "sensitivity": self.sensitivity,
            "subject_display_name": self.subject_display_name,
            "subject_entity_key": self.subject_entity_key,
            "subject_entity_type": self.subject_entity_type,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimCorrectionReplacement:
    object_kind: str
    object_literal: str | None
    object_entity_type: str | None
    object_display_name: str | None
    epistemic_state: str
    sensitivity: str

    def __post_init__(self) -> None:
        require_key(self.object_kind, "invalid_correction_object_kind")
        require_key(self.epistemic_state, "invalid_correction_epistemic_state")
        require_key(self.sensitivity, "invalid_correction_sensitivity")
        if self.object_entity_type is not None:
            require_key(
                self.object_entity_type,
                "invalid_correction_object_entity_type",
            )
        for value, code in (
            (self.object_literal, "invalid_correction_object_literal"),
            (self.object_display_name, "invalid_correction_object_display_name"),
        ):
            if value is not None and not isinstance(value, str):
                raise ContractViolation(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestSuccessorReceipt:
    owner_user_id: UUID
    operation_id: UUID
    bridge_source_binding_sha256: str
    decision: EligibilityDecision
    evidence_id: UUID
    extraction_job_id: UUID
    receipt_sha256: str

    def __post_init__(self) -> None:
        for value, code in (
            (self.owner_user_id, "invalid_ingest_receipt_owner"),
            (self.operation_id, "invalid_ingest_receipt_operation"),
            (self.evidence_id, "invalid_ingest_receipt_evidence"),
            (self.extraction_job_id, "invalid_ingest_receipt_job"),
        ):
            require_uuid(value, code)
        if self.decision is not EligibilityDecision.SEND_EXTERNAL:
            raise ContractViolation("invalid_ingest_receipt_decision")
        require_sha256(
            self.bridge_source_binding_sha256,
            "invalid_ingest_receipt_bridge_binding",
        )
        expected = ingest_successor_receipt_sha256(
            owner_user_id=self.owner_user_id,
            operation_id=self.operation_id,
            bridge_source_binding_sha256=self.bridge_source_binding_sha256,
            decision=self.decision.value,
            evidence_id=self.evidence_id,
            extraction_job_id=self.extraction_job_id,
        )
        if require_sha256(
            self.receipt_sha256,
            "invalid_ingest_receipt_sha256",
        ) != expected:
            raise ContractViolation("ingest_successor_receipt_sha256_mismatch")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyReceipt:
    write_kind: RepositoryWriteKind
    operation_id: UUID
    outcome: OperationOutcome
    material_sha256: str
    rows_written: int

    def __post_init__(self) -> None:
        if not isinstance(self.write_kind, RepositoryWriteKind):
            raise ContractViolation("invalid_repository_write_kind")
        if not isinstance(self.outcome, OperationOutcome):
            raise ContractViolation("invalid_apply_outcome")
        require_uuid(self.operation_id, "invalid_apply_operation")
        require_sha256(self.material_sha256, "invalid_apply_material_sha256")
        require_exact_int(
            self.rows_written,
            code="invalid_apply_rows_written",
            minimum=0,
            maximum=10_000,
        )


class GovernedMemoryRepository(Protocol):
    """No method accepts free-form plans, SQL, effect names, or caller clocks."""

    async def persist_ingest_decision(
        self,
        *,
        operation_id: UUID,
        owner_user_id: UUID,
        bridge_source_binding_sha256: str,
        source_message_id: UUID,
        source_thread_id: UUID,
        source_window_id: UUID,
        window_sha256: str,
        source_sha256: str,
        selected_sha256: str,
        selected_start_utf8: int,
        selected_end_utf8: int,
        context_message_id: UUID | None,
        context_sha256: str | None,
        source_created_at: datetime,
        decision: EligibilityDecision,
        policy_sha256: str,
        review_excerpt: str | None,
    ) -> ApplyReceipt: ...

    async def read_ingest_receipt(
        self,
        *,
        owner_user_id: UUID,
        operation_id: UUID,
        bridge_source_binding_sha256: str,
    ) -> IngestSuccessorReceipt | None: ...

    async def fail_ingest(
        self,
        *,
        outbox_id: UUID,
        lease_token: UUID,
        failure: BridgeFailure,
    ) -> ApplyReceipt: ...

    async def mark_provider_dispatched(
        self,
        *,
        job_id: UUID,
        provider_call_id: UUID,
        lease_token: UUID,
        request_sha256: str,
        expected_selected_sha256: str,
        expected_selection_binding_sha256: str,
        expected_predicate_catalog_sha256: str,
    ) -> ApplyReceipt: ...

    async def complete_provider_call(
        self,
        *,
        job_id: UUID,
        provider_call_id: UUID,
        lease_token: UUID,
        request_sha256: str,
        response_sha256: str,
        input_tokens: int,
        output_tokens: int,
        proposals: Sequence[CompleteExtractionProposal],
    ) -> ApplyReceipt: ...

    async def fail_provider_call(
        self,
        *,
        job_id: UUID,
        provider_call_id: UUID,
        lease_token: UUID,
        failure: ProviderFailure,
    ) -> ApplyReceipt: ...

    async def apply_proposal_review(
        self,
        *,
        operation_id: UUID,
        proposal_id: UUID,
        decision: ReviewDecision,
        expected_proposal_sha256: str,
        expected_source_sha256: str,
        expected_selected_sha256: str,
        expected_selection_binding_sha256: str,
        expected_predicate_catalog_sha256: str,
        reason_codes: Sequence[str],
    ) -> ApplyReceipt: ...

    async def correct_claim(
        self,
        *,
        operation_id: UUID,
        claim_id: UUID,
        expected_state_sha256: str,
        expected_revision_sha256: str,
        expected_predicate_catalog_sha256: str,
        replacement: ClaimCorrectionReplacement,
    ) -> ApplyReceipt: ...

    async def retract_claim(
        self,
        *,
        operation_id: UUID,
        claim_id: UUID,
        expected_revision_sha256: str,
        expected_state_sha256: str,
    ) -> ApplyReceipt: ...

    async def request_claim_deletion(
        self,
        *,
        operation_id: UUID,
        claim_id: UUID,
        expected_revision_sha256: str,
        expected_state_sha256: str,
    ) -> ApplyReceipt: ...

    async def finalize_claim_deletion(
        self,
        *,
        operation_id: UUID,
        owner_user_id: UUID,
        claim_id: UUID,
        expected_state_sha256: str,
        delete_outbox_id: UUID,
        expected_revision_id: UUID,
        expected_revision_sha256: str,
        expected_sequence_number: int,
        projection_manifest_sha256: str,
        physical_collection_name: str,
        applied_at: datetime,
        verified_at: datetime,
        verification_sha256: str,
        verification_receipt_sha256: str,
    ) -> ApplyReceipt: ...

    async def finish_projection(
        self,
        *,
        outbox_id: UUID,
        lease_token: UUID,
        completion: ProjectionCompletion,
    ) -> ApplyReceipt: ...

    async def persist_answer_binding(
        self,
        *,
        operation_id: UUID,
        owner_user_id: UUID,
        response_id: UUID,
        thread_id: UUID,
        query_sha256: str,
        policy_sha256: str,
        allowed_predicates: Sequence[str],
        domains: Sequence[str],
        intents: Sequence[str],
        max_records: int,
        policy_revision: int,
        renderer_sha256: str,
        prompt_sha256: str,
        explicit_recall: bool,
        selected_claim_ids: Sequence[UUID],
        injected_claim_ids: Sequence[UUID],
        outcome: AnswerBindingOutcome,
        expected_selection_manifest_sha256: str,
        expected_injection_manifest_sha256: str | None,
        memory_block: str | None,
        outbound_request: str | None,
    ) -> ApplyReceipt: ...

    async def read_claims(
        self, *, owner_user_id: UUID, claim_ids: Sequence[UUID]
    ) -> tuple[Mapping[str, Any], ...]: ...

    async def read_operation(
        self, *, owner_user_id: UUID, operation_id: UUID
    ) -> Mapping[str, Any] | None: ...


REPOSITORY_STORED_PROCEDURE_MAP = MappingProxyType(
    {
        "persist_ingest_decision": (
            "memory_private.record_selected_evidence",
            (
                "owner_user_id",
                "operation_id",
                "bridge_source_binding_sha256",
                "message_id",
                "thread_id",
                "window_id",
                "window_sha256",
                "source_sha256",
                "selected_sha256",
                "selected_start_utf8",
                "selected_end_utf8",
                "context_message_id",
                "context_sha256",
                "source_created_at",
                "decision",
                "policy_sha256",
                "review_excerpt",
            ),
        ),
        "read_ingest_receipt": (
            "memory_private.read_ingest_receipt",
            (
                "owner_user_id",
                "operation_id",
                "bridge_source_binding_sha256",
            ),
        ),
        "fail_ingest": (
            "memory_ingest_private.fail_memory_ingest",
            (
                "outbox_id",
                "lease_token",
                "failure_mode",
                "error_code",
                "retry_after_seconds",
            ),
        ),
        "mark_provider_dispatched": (
            "memory_private.mark_provider_call_dispatched",
            (
                "job_id",
                "lease_token",
                "provider_call_id",
                "request_sha256",
                "expected_selected_sha256",
                "expected_selection_binding_sha256",
                "expected_predicate_catalog_sha256",
            ),
        ),
        "complete_provider_call": (
            "memory_private.complete_extraction",
            (
                "job_id",
                "lease_token",
                "provider_call_id",
                "outcome",
                "request_sha256",
                "response_sha256",
                "input_tokens",
                "output_tokens",
                "error_code",
                "proposals",
            ),
        ),
        "fail_provider_call": (
            "memory_private.complete_extraction",
            (
                "job_id",
                "lease_token",
                "provider_call_id",
                "outcome",
                "request_sha256",
                "response_sha256",
                "input_tokens",
                "output_tokens",
                "error_code",
                "proposals",
            ),
        ),
        "apply_proposal_review": (
            "memory_private.review_proposal",
            (
                "operation_id",
                "proposal_id",
                "decision",
                "expected_proposal_sha256",
                "expected_source_sha256",
                "expected_selected_sha256",
                "expected_selection_binding_sha256",
                "expected_predicate_catalog_sha256",
                "reason_codes",
            ),
        ),
        "correct_claim": (
            "memory_private.correct_claim",
            (
                "operation_id",
                "claim_id",
                "expected_revision_sha256",
                "expected_state_sha256",
                "expected_predicate_catalog_sha256",
                "replacement",
            ),
        ),
        "retract_claim": (
            "memory_private.retract_claim",
            (
                "operation_id",
                "claim_id",
                "expected_revision_sha256",
                "expected_state_sha256",
            ),
        ),
        "request_claim_deletion": (
            "memory_private.request_claim_deletion",
            (
                "operation_id",
                "claim_id",
                "expected_revision_sha256",
                "expected_state_sha256",
            ),
        ),
        "finish_projection": (
            "memory_private.finish_projection_job",
            (
                "outbox_id",
                "lease_token",
                "outcome",
                "physical_collection_name",
                "vector_sha256",
                "verification_sha256",
                "verification_receipt_sha256",
                "error_code",
            ),
        ),
        "finalize_claim_deletion": (
            "memory_private.finalize_claim_deletion",
            (
                "operation_id",
                "owner_user_id",
                "claim_id",
                "expected_state_sha256",
                "delete_outbox_id",
                "expected_revision_id",
                "expected_revision_sha256",
                "expected_sequence_number",
                "projection_manifest_sha256",
                "physical_collection_name",
                "applied_at",
                "verified_at",
                "verification_sha256",
                "verification_receipt_sha256",
            ),
        ),
        "persist_answer_binding": (
            "memory_private.record_answer_binding",
            (
                "operation_id",
                "response_id",
                "thread_id",
                "query_sha256",
                "policy_sha256",
                "allowed_predicates",
                "domains",
                "intents",
                "max_records",
                "policy_revision",
                "renderer_sha256",
                "prompt_sha256",
                "explicit_recall",
                "selected_claim_ids",
                "injected_claim_ids",
                "outcome",
                "expected_selection_manifest_sha256",
                "expected_injection_manifest_sha256",
                "memory_block",
                "outbound_request",
            ),
        ),
    }
)


__all__ = [
    "AnswerBindingOutcome",
    "ApplyReceipt",
    "BridgeFailure",
    "BridgeFailureMode",
    "BridgeFailureReason",
    "ClaimCorrectionReplacement",
    "CompleteExtractionProposal",
    "GovernedMemoryRepository",
    "IngestSuccessorReceipt",
    "ProviderFailureDisposition",
    "ProviderFailure",
    "ProviderFailureReason",
    "ProjectionCompletionOutcome",
    "ProjectionCompletion",
    "ProjectionFailureReason",
    "REPOSITORY_STORED_PROCEDURE_MAP",
    "RepositoryWriteKind",
]
