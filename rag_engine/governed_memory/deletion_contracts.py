from __future__ import annotations

"""Closed, content-free contracts for coordinated chat-source erasure.

The caller cannot provide an owner, cutoff, or timestamp.  PostgreSQL resolves
the selector against its transaction clock and returns an immutable selector
hash plus a complete content-free target manifest.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Mapping
from uuid import UUID

from .auth import ActorRole, VerifiedActor
from .contracts import (
    ContractViolation,
    canonical_sha256,
    framed_sha256,
    require_exact_int,
    require_sha256,
    require_utc,
    require_uuid,
    sha256_text,
)


DELETION_REQUEST_CONTRACT_VERSION = (
    "governed-memory-conversation-deletion-request-v1"
)
CONVERSATIONAL_ERASURE_DOMAIN = (
    "chat_source_and_derived_governed_conversational_memory_v1"
)
MIN_RECENT_WINDOW_SECONDS = 60
MAX_RECENT_WINDOW_SECONDS = 31 * 24 * 60 * 60
ALLOWED_RECENT_WINDOW_SECONDS = (3600, 86400, 604800, 2592000)
MAX_ERASURE_TARGETS = 100_000
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class DeletionSelectorKind(str, Enum):
    MESSAGE_TAIL = "message_tail"
    THREAD = "thread"
    RECENT = "recent"
    ALL_CONVERSATIONS = "all_conversations"


class ConversationErasureState(str, Enum):
    FENCED = "fenced"
    GOVERNED_DELETION_PENDING = "governed_deletion_pending"
    GOVERNED_DELETED = "governed_deleted"
    CONVERSATION_DELETED_PENDING_ACK = "conversation_deleted_pending_ack"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    MANUAL_REVIEW = "manual_review"


class SuccessorErasureState(str, Enum):
    RECEIVING = "receiving"
    FENCED = "fenced"
    CLAIM_DELETION_PENDING = "claim_deletion_pending"
    MEMORY_DELETED = "memory_deleted"
    COMPLETED = "completed"
    MANUAL_REVIEW = "manual_review"


class DeletionMutationOutcome(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"


class ClaimDeletionStepOutcome(str, Enum):
    PREPARED = "prepared"
    NO_NEW_CLAIM_DELETIONS = "no_new_claim_deletions"


class DeletionCoordinatorOutcome(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    MANUAL_REVIEW = "manual_review"


def _canonical_uuid_text(value: object, code: str) -> UUID:
    if not isinstance(value, str):
        raise ContractViolation(code)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ContractViolation(code) from exc
    if str(parsed) != value:
        raise ContractViolation(code)
    return parsed


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationDeletionRequestV1:
    """Caller fields for chat source plus memory derived from that source.

    The fixed domain excludes accounts, LifeSwitch libraries, workouts,
    weightlifting sessions, food logs, measurements, and every other structured
    tracking surface.  There is deliberately no standalone memory-only scope.
    """

    operation_id: UUID
    selector_kind: DeletionSelectorKind
    anchor_message_id: UUID | None = None
    thread_id: UUID | None = None
    recent_window_seconds: int | None = None
    confirmation_sha256: str = ""
    contract_version: str = DELETION_REQUEST_CONTRACT_VERSION
    data_domain: str = CONVERSATIONAL_ERASURE_DOMAIN

    def __post_init__(self) -> None:
        if self.contract_version != DELETION_REQUEST_CONTRACT_VERSION:
            raise ContractViolation("invalid_deletion_request_contract")
        if self.data_domain != CONVERSATIONAL_ERASURE_DOMAIN:
            raise ContractViolation("invalid_deletion_data_domain")
        require_uuid(self.operation_id, "invalid_deletion_operation")
        if not isinstance(self.selector_kind, DeletionSelectorKind):
            raise ContractViolation("invalid_deletion_selector_kind")
        if self.anchor_message_id is not None:
            require_uuid(
                self.anchor_message_id, "invalid_deletion_anchor_message"
            )
        if self.thread_id is not None:
            require_uuid(self.thread_id, "invalid_deletion_thread")
        if self.recent_window_seconds is not None:
            require_exact_int(
                self.recent_window_seconds,
                code="invalid_deletion_recent_window",
                minimum=MIN_RECENT_WINDOW_SECONDS,
                maximum=MAX_RECENT_WINDOW_SECONDS,
            )
            if self.recent_window_seconds not in (
                ALLOWED_RECENT_WINDOW_SECONDS
            ):
                raise ContractViolation("invalid_deletion_recent_window")
        require_sha256(
            self.confirmation_sha256, "invalid_deletion_confirmation"
        )
        shapes = {
            DeletionSelectorKind.MESSAGE_TAIL: (
                self.anchor_message_id is not None
                and self.thread_id is not None
                and self.recent_window_seconds is None
            ),
            DeletionSelectorKind.THREAD: (
                self.anchor_message_id is None
                and self.thread_id is not None
                and self.recent_window_seconds is None
            ),
            DeletionSelectorKind.RECENT: (
                self.anchor_message_id is None
                and self.thread_id is None
                and self.recent_window_seconds is not None
            ),
            DeletionSelectorKind.ALL_CONVERSATIONS: (
                self.anchor_message_id is None
                and self.thread_id is None
                and self.recent_window_seconds is None
            ),
        }
        if shapes[self.selector_kind] is not True:
            raise ContractViolation("invalid_deletion_selector_shape")

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.conversation_deletion_request.v1",
            {
                "contract_version": self.contract_version,
                "confirmation_sha256": self.confirmation_sha256,
                "data_domain": self.data_domain,
                "anchor_message_id": self.anchor_message_id,
                "operation_id": self.operation_id,
                "recent_window_seconds": self.recent_window_seconds,
                "selector_kind": self.selector_kind,
                "thread_id": self.thread_id,
            },
        )


def conversation_deletion_request_from_body(
    body: Mapping[str, object],
) -> ConversationDeletionRequestV1:
    if not isinstance(body, Mapping) or any(
        not isinstance(key, str) for key in body
    ):
        raise ContractViolation("invalid_deletion_request_body")
    if any(
        field in body
        for field in (
            "owner_user_id",
            "user_id",
            "requested_at",
            "cutoff_at",
            "not_before",
            "before",
        )
    ):
        raise ContractViolation("deletion_request_authority_field_prohibited")
    required = {
        "contract_version",
        "data_domain",
        "confirmation_sha256",
        "operation_id",
        "selector_kind",
    }
    optional = {
        "anchor_message_id",
        "thread_id",
        "recent_window_seconds",
    }
    keys = set(body)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ContractViolation("deletion_request_body_contract_mismatch")
    try:
        selector_kind = DeletionSelectorKind(body["selector_kind"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_deletion_selector_kind") from exc
    return ConversationDeletionRequestV1(
        contract_version=body["contract_version"],
        data_domain=body["data_domain"],
        confirmation_sha256=body["confirmation_sha256"],
        operation_id=_canonical_uuid_text(
            body["operation_id"], "invalid_deletion_operation"
        ),
        selector_kind=selector_kind,
        anchor_message_id=(
            _canonical_uuid_text(
                body["anchor_message_id"],
                "invalid_deletion_anchor_message",
            )
            if "anchor_message_id" in body
            else None
        ),
        thread_id=(
            _canonical_uuid_text(body["thread_id"], "invalid_deletion_thread")
            if "thread_id" in body
            else None
        ),
        recent_window_seconds=body.get("recent_window_seconds"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionAuthority:
    owner_user_id: UUID
    actor_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_deletion_owner")
        require_uuid(self.actor_id, "invalid_deletion_actor")
        require_uuid(self.session_id, "invalid_deletion_session")
        if self.actor_id != self.owner_user_id:
            raise ContractViolation("deletion_owner_actor_mismatch")
        require_sha256(
            self.authentication_manifest_sha256,
            "invalid_deletion_authentication_manifest",
        )

    @classmethod
    def from_verified_actor(cls, actor: VerifiedActor) -> "DeletionAuthority":
        if not isinstance(actor, VerifiedActor):
            raise ContractViolation("unverified_deletion_actor")
        if actor.role is not ActorRole.OWNER:
            raise ContractViolation("deletion_owner_authority_required")
        return cls(
            owner_user_id=actor.owner_user_id,
            actor_id=actor.actor_id,
            session_id=actor.session_id,
            authentication_manifest_sha256=(
                actor.authentication_manifest_sha256
            ),
        )


def deletion_binding_sha256(
    *,
    owner_user_id: UUID,
    operation_id: UUID,
    request_sha256: str,
) -> str:
    require_uuid(owner_user_id, "invalid_deletion_owner")
    require_uuid(operation_id, "invalid_deletion_operation")
    return canonical_sha256(
        "governed_memory.conversation_deletion_binding.v1",
        {
            "operation_id": operation_id,
            "owner_user_id": owner_user_id,
            "request_sha256": require_sha256(
                request_sha256, "invalid_deletion_request_sha256"
            ),
        },
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundConversationDeletion:
    authority: DeletionAuthority
    request: ConversationDeletionRequestV1
    binding_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, DeletionAuthority):
            raise ContractViolation("invalid_deletion_authority")
        if not isinstance(self.request, ConversationDeletionRequestV1):
            raise ContractViolation("invalid_deletion_request")
        expected = deletion_binding_sha256(
            owner_user_id=self.authority.owner_user_id,
            operation_id=self.request.operation_id,
            request_sha256=self.request.request_sha256,
        )
        if require_sha256(
            self.binding_sha256, "invalid_deletion_binding_sha256"
        ) != expected:
            raise ContractViolation("deletion_binding_sha256_mismatch")

    @property
    def operation_id(self) -> UUID:
        return self.request.operation_id

    @property
    def request_sha256(self) -> str:
        return self.request.request_sha256


def bind_conversation_deletion(
    *,
    actor: VerifiedActor,
    request: ConversationDeletionRequestV1,
) -> BoundConversationDeletion:
    if not isinstance(request, ConversationDeletionRequestV1):
        raise ContractViolation("invalid_deletion_request")
    authority = DeletionAuthority.from_verified_actor(actor)
    return BoundConversationDeletion(
        authority=authority,
        request=request,
        binding_sha256=deletion_binding_sha256(
            owner_user_id=authority.owner_user_id,
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
        ),
    )


def source_erasure_target_sha256(
    *,
    owner_user_id: UUID,
    operation_id: UUID,
    message_id: UUID,
    thread_id: UUID,
    source_created_at: datetime,
) -> str:
    for value, code in (
        (owner_user_id, "invalid_erasure_target_owner"),
        (operation_id, "invalid_erasure_target_operation"),
        (message_id, "invalid_erasure_target_message"),
        (thread_id, "invalid_erasure_target_thread"),
    ):
        require_uuid(value, code)
    from .contracts import utc_text

    return framed_sha256(
        "governed_memory.source_erasure_target.v1",
        (
            ("owner_user_id", str(owner_user_id)),
            ("operation_id", str(operation_id)),
            ("message_id", str(message_id)),
            ("thread_id", str(thread_id)),
            (
                "source_created_at",
                utc_text(
                    require_utc(
                        source_created_at,
                        "invalid_erasure_target_source_created_at",
                    )
                ),
            ),
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationErasureTarget:
    owner_user_id: UUID
    operation_id: UUID
    message_id: UUID
    thread_id: UUID
    source_created_at: datetime
    target_sha256: str

    def __post_init__(self) -> None:
        expected = source_erasure_target_sha256(
            owner_user_id=self.owner_user_id,
            operation_id=self.operation_id,
            message_id=self.message_id,
            thread_id=self.thread_id,
            source_created_at=self.source_created_at,
        )
        if require_sha256(
            self.target_sha256, "invalid_erasure_target_sha256"
        ) != expected:
            raise ContractViolation("erasure_target_sha256_mismatch")


def erasure_target_manifest_sha256(
    *,
    owner_user_id: UUID,
    operation_id: UUID,
    targets: tuple[ConversationErasureTarget, ...],
) -> str:
    require_uuid(owner_user_id, "invalid_erasure_manifest_owner")
    require_uuid(operation_id, "invalid_erasure_manifest_operation")
    if not isinstance(targets, tuple) or len(targets) > MAX_ERASURE_TARGETS:
        raise ContractViolation("invalid_erasure_targets")
    if any(
        not isinstance(target, ConversationErasureTarget)
        for target in targets
    ):
        raise ContractViolation("invalid_erasure_target")
    if any(
        target.owner_user_id != owner_user_id
        or target.operation_id != operation_id
        for target in targets
    ):
        raise ContractViolation("erasure_target_binding_mismatch")
    keys = tuple(
        (require_utc(target.source_created_at), str(target.message_id))
        for target in targets
    )
    if keys != tuple(sorted(keys)):
        raise ContractViolation("erasure_targets_not_sorted")
    if len({target.message_id for target in targets}) != len(targets):
        raise ContractViolation("duplicate_erasure_target_message")
    return sha256_text(
        "governed_memory.source_erasure_target_manifest.v1\n"
        + "\n".join(target.target_sha256 for target in targets)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationErasureStatus:
    owner_user_id: UUID
    operation_id: UUID
    selector_kind: DeletionSelectorKind
    state: ConversationErasureState
    target_count: int
    selector_sha256: str
    target_manifest_sha256: str
    governed_receipt_sha256: str | None
    last_error_code: str | None
    created_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_erasure_status_owner")
        require_uuid(self.operation_id, "invalid_erasure_status_operation")
        if not isinstance(self.selector_kind, DeletionSelectorKind):
            raise ContractViolation("invalid_erasure_status_selector_kind")
        if not isinstance(self.state, ConversationErasureState):
            raise ContractViolation("invalid_conversation_erasure_state")
        require_exact_int(
            self.target_count,
            code="invalid_erasure_status_target_count",
            maximum=MAX_ERASURE_TARGETS,
        )
        require_sha256(
            self.selector_sha256, "invalid_erasure_status_selector_sha256"
        )
        require_sha256(
            self.target_manifest_sha256,
            "invalid_erasure_status_target_manifest",
        )
        if self.governed_receipt_sha256 is not None:
            require_sha256(
                self.governed_receipt_sha256,
                "invalid_governed_erasure_receipt",
            )
        if self.state in {
            ConversationErasureState.GOVERNED_DELETED,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        } and self.governed_receipt_sha256 is None:
            raise ContractViolation("missing_governed_erasure_receipt")
        if self.last_error_code is not None and not _ERROR_CODE_RE.fullmatch(
            self.last_error_code
        ):
            raise ContractViolation("invalid_erasure_status_error_code")
        require_utc(self.created_at, "invalid_erasure_status_created_at")
        if self.completed_at is not None:
            require_utc(
                self.completed_at, "invalid_erasure_status_completed_at"
            )
        if self.state in {
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
            ConversationErasureState.COMPLETED,
        }:
            if self.completed_at is None or self.governed_receipt_sha256 is None:
                raise ContractViolation("incomplete_erasure_completion_status")
        elif self.completed_at is not None:
            raise ContractViolation("premature_erasure_completion_timestamp")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationErasureLease:
    owner_user_id: UUID
    operation_id: UUID
    selector_kind: DeletionSelectorKind
    selector_sha256: str
    target_count: int
    target_manifest_sha256: str
    state: ConversationErasureState
    governed_receipt_sha256: str | None
    lease_token: UUID

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_erasure_lease_owner")
        require_uuid(self.operation_id, "invalid_erasure_lease_operation")
        if not isinstance(self.selector_kind, DeletionSelectorKind):
            raise ContractViolation("invalid_erasure_lease_selector")
        require_sha256(
            self.selector_sha256, "invalid_erasure_lease_selector_sha256"
        )
        require_exact_int(
            self.target_count,
            code="invalid_erasure_lease_target_count",
            maximum=MAX_ERASURE_TARGETS,
        )
        require_sha256(
            self.target_manifest_sha256,
            "invalid_erasure_lease_target_manifest",
        )
        if not isinstance(self.state, ConversationErasureState):
            raise ContractViolation("invalid_erasure_lease_state")
        if self.governed_receipt_sha256 is not None:
            require_sha256(
                self.governed_receipt_sha256,
                "invalid_erasure_lease_governed_receipt",
            )
        if self.state in {
            ConversationErasureState.GOVERNED_DELETED,
            ConversationErasureState.CONVERSATION_DELETED_PENDING_ACK,
        } and self.governed_receipt_sha256 is None:
            raise ContractViolation("missing_erasure_lease_governed_receipt")
        require_uuid(self.lease_token, "invalid_erasure_lease_token")

    @property
    def work_binding_sha256(self) -> str:
        return source_erasure_work_binding_sha256(
            owner_user_id=self.owner_user_id,
            operation_id=self.operation_id,
            selector_kind=self.selector_kind,
            selector_sha256=self.selector_sha256,
            target_count=self.target_count,
            target_manifest_sha256=self.target_manifest_sha256,
        )


def source_erasure_work_binding_sha256(
    *,
    owner_user_id: UUID,
    operation_id: UUID,
    selector_kind: DeletionSelectorKind,
    selector_sha256: str,
    target_count: int,
    target_manifest_sha256: str,
) -> str:
    require_uuid(owner_user_id, "invalid_erasure_work_owner")
    require_uuid(operation_id, "invalid_erasure_work_operation")
    if not isinstance(selector_kind, DeletionSelectorKind):
        raise ContractViolation("invalid_erasure_work_selector_kind")
    require_exact_int(
        target_count,
        code="invalid_erasure_work_target_count",
        maximum=MAX_ERASURE_TARGETS,
    )
    return canonical_sha256(
        "governed_memory.source_erasure_work_binding.v1",
        {
            "operation_id": operation_id,
            "owner_user_id": owner_user_id,
            "selector_kind": selector_kind,
            "selector_sha256": require_sha256(
                selector_sha256, "invalid_erasure_work_selector_sha256"
            ),
            "target_count": target_count,
            "target_manifest_sha256": require_sha256(
                target_manifest_sha256,
                "invalid_erasure_work_target_manifest",
            ),
        },
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationFinalizationReceipt:
    owner_user_id: UUID
    operation_id: UUID
    outcome: DeletionMutationOutcome
    receipt_sha256: str
    deleted_message_count: int
    deleted_thread_count: int
    deleted_attachment_count: int
    deleted_bridge_row_count: int
    completed_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_finalization_owner")
        require_uuid(self.operation_id, "invalid_finalization_operation")
        if not isinstance(self.outcome, DeletionMutationOutcome):
            raise ContractViolation("invalid_finalization_outcome")
        require_sha256(
            self.receipt_sha256, "invalid_finalization_receipt_sha256"
        )
        require_exact_int(
            self.deleted_message_count,
            code="invalid_deleted_message_count",
            maximum=MAX_ERASURE_TARGETS,
        )
        require_exact_int(
            self.deleted_thread_count,
            code="invalid_deleted_thread_count",
            maximum=MAX_ERASURE_TARGETS,
        )
        require_exact_int(
            self.deleted_attachment_count,
            code="invalid_deleted_attachment_count",
            maximum=1_000_000,
        )
        require_exact_int(
            self.deleted_bridge_row_count,
            code="invalid_deleted_bridge_row_count",
            maximum=MAX_ERASURE_TARGETS,
        )
        require_utc(self.completed_at, "invalid_finalization_completed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionMutationReceipt:
    owner_user_id: UUID
    operation_id: UUID
    outcome: DeletionMutationOutcome
    material_sha256: str

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_deletion_receipt_owner")
        require_uuid(self.operation_id, "invalid_deletion_receipt_operation")
        if not isinstance(self.outcome, DeletionMutationOutcome):
            raise ContractViolation("invalid_deletion_mutation_outcome")
        require_sha256(
            self.material_sha256, "invalid_deletion_receipt_material"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SuccessorErasureProgress:
    owner_user_id: UUID
    operation_id: UUID
    state: SuccessorErasureState
    target_count: int
    touched_claim_count: int
    prepared_claim_count: int
    deleted_claim_count: int
    target_manifest_sha256: str
    governed_receipt_sha256: str | None
    conversation_receipt_sha256: str | None
    last_error_code: str | None

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_successor_progress_owner")
        require_uuid(self.operation_id, "invalid_successor_progress_operation")
        if not isinstance(self.state, SuccessorErasureState):
            raise ContractViolation("invalid_successor_erasure_state")
        counts = (
            self.target_count,
            self.touched_claim_count,
            self.prepared_claim_count,
            self.deleted_claim_count,
        )
        for value in counts:
            require_exact_int(
                value,
                code="invalid_successor_erasure_count",
                maximum=MAX_ERASURE_TARGETS,
            )
        if not (
            self.deleted_claim_count
            <= self.prepared_claim_count
            <= self.touched_claim_count
        ):
            raise ContractViolation("successor_erasure_claim_count_mismatch")
        if self.state in {
            SuccessorErasureState.MEMORY_DELETED,
            SuccessorErasureState.COMPLETED,
        } and self.deleted_claim_count != self.touched_claim_count:
            raise ContractViolation("successor_erasure_not_ready")
        require_sha256(
            self.target_manifest_sha256,
            "invalid_successor_target_manifest",
        )
        if self.governed_receipt_sha256 is not None:
            require_sha256(
                self.governed_receipt_sha256,
                "invalid_successor_governed_receipt",
            )
        if self.conversation_receipt_sha256 is not None:
            require_sha256(
                self.conversation_receipt_sha256,
                "invalid_successor_conversation_receipt",
            )
        if self.state is SuccessorErasureState.MEMORY_DELETED and (
            self.governed_receipt_sha256 is None
        ):
            raise ContractViolation("missing_successor_governed_receipt")
        if self.state is SuccessorErasureState.COMPLETED and (
            self.governed_receipt_sha256 is None
            or self.conversation_receipt_sha256 is None
        ):
            raise ContractViolation("missing_successor_completion_receipts")
        if self.last_error_code is not None and not _ERROR_CODE_RE.fullmatch(
            self.last_error_code
        ):
            raise ContractViolation("invalid_successor_erasure_error_code")

    @property
    def affected_claim_count(self) -> int:
        return self.touched_claim_count

    @property
    def pending_claim_deletions(self) -> int:
        return self.touched_claim_count - self.deleted_claim_count

    @property
    def completed_claim_deletions(self) -> int:
        return self.deleted_claim_count


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceErasureReceipt:
    operation_id: UUID
    selector_sha256: str
    target_manifest_sha256: str
    target_count: int
    touched_claim_count: int
    claim_deletion_receipt_count: int
    pre_fence_provider_dispatch_count: int
    governed_absence_sha256: str
    conversation_receipt_sha256: str
    receipt_sha256: str
    completed_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.operation_id, "invalid_source_erasure_operation")
        for value, code in (
            (self.selector_sha256, "invalid_source_erasure_selector"),
            (
                self.target_manifest_sha256,
                "invalid_source_erasure_target_manifest",
            ),
            (
                self.governed_absence_sha256,
                "invalid_source_erasure_absence_receipt",
            ),
            (
                self.conversation_receipt_sha256,
                "invalid_source_erasure_conversation_receipt",
            ),
            (self.receipt_sha256, "invalid_source_erasure_receipt"),
        ):
            require_sha256(value, code)
        for value in (
            self.target_count,
            self.touched_claim_count,
            self.claim_deletion_receipt_count,
            self.pre_fence_provider_dispatch_count,
        ):
            require_exact_int(
                value,
                code="invalid_source_erasure_receipt_count",
                maximum=MAX_ERASURE_TARGETS,
            )
        if self.claim_deletion_receipt_count != self.touched_claim_count:
            raise ContractViolation("source_erasure_receipt_count_mismatch")
        require_utc(self.completed_at, "invalid_source_erasure_completed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimDeletionStepReceipt:
    owner_user_id: UUID
    operation_id: UUID
    outcome: ClaimDeletionStepOutcome
    material_sha256: str

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_claim_step_owner")
        require_uuid(self.operation_id, "invalid_claim_step_operation")
        if not isinstance(self.outcome, ClaimDeletionStepOutcome):
            raise ContractViolation("invalid_claim_step_outcome")
        require_sha256(self.material_sha256, "invalid_claim_step_material")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionCoordinatorReceipt:
    owner_user_id: UUID
    operation_id: UUID
    outcome: DeletionCoordinatorOutcome
    conversation_state: ConversationErasureState
    successor_state: SuccessorErasureState
    target_count: int
    affected_claim_count: int
    pending_claim_deletions: int
    binding_sha256: str
    selector_sha256: str
    target_manifest_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_coordinator_receipt_owner")
        require_uuid(
            self.operation_id, "invalid_coordinator_receipt_operation"
        )
        if not isinstance(self.outcome, DeletionCoordinatorOutcome):
            raise ContractViolation("invalid_coordinator_outcome")
        if not isinstance(self.conversation_state, ConversationErasureState):
            raise ContractViolation("invalid_coordinator_conversation_state")
        if not isinstance(self.successor_state, SuccessorErasureState):
            raise ContractViolation("invalid_coordinator_successor_state")
        for value in (
            self.target_count,
            self.affected_claim_count,
            self.pending_claim_deletions,
        ):
            require_exact_int(
                value,
                code="invalid_coordinator_count",
                maximum=MAX_ERASURE_TARGETS,
            )
        if self.pending_claim_deletions > self.affected_claim_count:
            raise ContractViolation("invalid_coordinator_pending_count")
        for value, code in (
            (self.binding_sha256, "invalid_coordinator_binding"),
            (self.selector_sha256, "invalid_coordinator_selector"),
            (
                self.target_manifest_sha256,
                "invalid_coordinator_target_manifest",
            ),
        ):
            require_sha256(value, code)
        expected = deletion_coordinator_receipt_sha256(
            owner_user_id=self.owner_user_id,
            operation_id=self.operation_id,
            outcome=self.outcome,
            conversation_state=self.conversation_state,
            successor_state=self.successor_state,
            target_count=self.target_count,
            affected_claim_count=self.affected_claim_count,
            pending_claim_deletions=self.pending_claim_deletions,
            binding_sha256=self.binding_sha256,
            selector_sha256=self.selector_sha256,
            target_manifest_sha256=self.target_manifest_sha256,
        )
        if require_sha256(
            self.receipt_sha256, "invalid_coordinator_receipt_sha256"
        ) != expected:
            raise ContractViolation("coordinator_receipt_sha256_mismatch")


def deletion_coordinator_receipt_sha256(
    *,
    owner_user_id: UUID,
    operation_id: UUID,
    outcome: DeletionCoordinatorOutcome,
    conversation_state: ConversationErasureState,
    successor_state: SuccessorErasureState,
    target_count: int,
    affected_claim_count: int,
    pending_claim_deletions: int,
    binding_sha256: str,
    selector_sha256: str,
    target_manifest_sha256: str,
) -> str:
    return canonical_sha256(
        "governed_memory.deletion_coordinator_receipt.v1",
        {
            "affected_claim_count": affected_claim_count,
            "binding_sha256": binding_sha256,
            "conversation_state": conversation_state,
            "operation_id": operation_id,
            "outcome": outcome,
            "owner_user_id": owner_user_id,
            "pending_claim_deletions": pending_claim_deletions,
            "selector_sha256": selector_sha256,
            "successor_state": successor_state,
            "target_count": target_count,
            "target_manifest_sha256": target_manifest_sha256,
        },
    )


__all__ = [
    "BoundConversationDeletion",
    "ALLOWED_RECENT_WINDOW_SECONDS",
    "CONVERSATIONAL_ERASURE_DOMAIN",
    "ClaimDeletionStepOutcome",
    "ClaimDeletionStepReceipt",
    "ConversationDeletionRequestV1",
    "ConversationErasureLease",
    "ConversationErasureState",
    "ConversationErasureStatus",
    "ConversationErasureTarget",
    "ConversationFinalizationReceipt",
    "DELETION_REQUEST_CONTRACT_VERSION",
    "DeletionAuthority",
    "DeletionCoordinatorOutcome",
    "DeletionCoordinatorReceipt",
    "DeletionMutationOutcome",
    "DeletionMutationReceipt",
    "DeletionSelectorKind",
    "MAX_ERASURE_TARGETS",
    "MAX_RECENT_WINDOW_SECONDS",
    "MIN_RECENT_WINDOW_SECONDS",
    "SuccessorErasureProgress",
    "SuccessorErasureState",
    "SourceErasureReceipt",
    "bind_conversation_deletion",
    "conversation_deletion_request_from_body",
    "deletion_binding_sha256",
    "deletion_coordinator_receipt_sha256",
    "erasure_target_manifest_sha256",
    "source_erasure_target_sha256",
    "source_erasure_work_binding_sha256",
]
