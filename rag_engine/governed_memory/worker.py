from __future__ import annotations

"""Pure ingestion/context planning and provider-attempt state reduction."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Mapping
from uuid import UUID

from .auth import ActorRole, ActorScope, VerifiedActor, require_owner, require_scope
from .contracts import (
    ContractViolation,
    EligibilityDecision,
    ExtractionJobState,
    bridge_source_binding_sha256,
    canonical_sha256,
    framed_sha256,
    require_bounded_text,
    require_exact_int,
    require_sha256,
    require_utc,
    selection_binding_sha256,
    sha256_text,
)
from .eligibility import (
    EligibilityPolicy,
    EligibilityReason,
    classify_eligibility,
    context_external_route_denial,
    evaluate_eligibility,
    external_send_privacy_denial,
)


BRIDGE_LEASE_FIELDS = (
    "outbox_id",
    "owner_user_id",
    "message_id",
    "thread_id",
    "exchange_id",
    "window_id",
    "window_ordinal",
    "window_sha256",
    "content_sha256",
    "source_binding_sha256",
    "policy_sha256",
    "source_created_at",
    "ingest_after",
    "context_review_count",
    "eligibility_decision",
    "lease_token",
    "lease_expires_at",
)
_BRIDGE_LEASE_SOURCE_FIELDS = BRIDGE_LEASE_FIELDS[:-2]
INGEST_SUCCESSOR_RECEIPT_FIELDS = (
    "owner_user_id",
    "operation_id",
    "bridge_source_binding_sha256",
    "decision",
    "evidence_id",
    "extraction_job_id",
)
INGEST_SUCCESSOR_RECEIPT_DOMAIN = "governed_memory.ingest_successor_receipt.v1"


def ingest_successor_receipt_sha256(
    owner_user_id: UUID,
    operation_id: UUID,
    bridge_source_binding_sha256: str,
    decision: str,
    evidence_id: UUID,
    extraction_job_id: UUID,
) -> str:
    owner = _uuid(owner_user_id, "invalid_ingest_receipt_owner")
    operation = _uuid(operation_id, "invalid_ingest_receipt_operation")
    evidence = _uuid(evidence_id, "invalid_ingest_receipt_evidence")
    job = _uuid(extraction_job_id, "invalid_ingest_receipt_job")
    if decision != EligibilityDecision.SEND_EXTERNAL.value:
        raise ContractViolation("invalid_ingest_receipt_decision")
    fields = (
        ("owner_user_id", str(owner)),
        ("operation_id", str(operation)),
        (
            "bridge_source_binding_sha256",
            require_sha256(
                bridge_source_binding_sha256,
                "invalid_ingest_receipt_bridge_binding",
            ),
        ),
        ("decision", decision),
        ("evidence_id", str(evidence)),
        ("extraction_job_id", str(job)),
    )
    if tuple(name for name, _ in fields) != INGEST_SUCCESSOR_RECEIPT_FIELDS:
        raise ContractViolation("ingest_successor_receipt_field_order_mismatch")
    return framed_sha256(INGEST_SUCCESSOR_RECEIPT_DOMAIN, fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedBridgeSource:
    outbox_id: UUID
    owner_user_id: UUID
    message_id: UUID
    thread_id: UUID
    exchange_id: UUID
    window_id: UUID
    window_ordinal: int
    window_sha256: str
    content_sha256: str
    source_binding_sha256: str
    policy_sha256: str
    source_created_at: datetime
    ingest_after: datetime
    context_review_count: int
    eligibility_decision: str | None

    @property
    def policy(self) -> EligibilityPolicy:
        return EligibilityPolicy(ingest_after=self.ingest_after)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedClaimedBridgeLease:
    source: VerifiedBridgeSource
    lease_token: UUID
    lease_expires_at: datetime


class ProviderCallState(str, Enum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ProviderAttemptAction(str, Enum):
    RESERVE = "reserve"
    MARK_DISPATCHED = "mark_dispatched"
    COMPLETE = "complete"
    FAIL_NOT_EXECUTED = "fail_not_executed"
    FAIL_TERMINAL = "fail_terminal"
    FAIL_UNKNOWN = "fail_unknown"
    LEASE_EXPIRED = "lease_expired"


NOT_EXECUTED_REASON_CODES = frozenset(
    {
        "adapter_rejected_before_send",
        "connection_failed_before_send",
        "evidence_excerpt_expired_before_dispatch",
        "local_serialization_failed_before_send",
        "provider_proved_not_accepted",
        "lease_expired_before_dispatch",
    }
)
TERMINAL_PROVIDER_REASON_CODES = frozenset(
    {
        "invalid_provider_output",
        "provider_auth_rejected",
        "provider_request_rejected",
        "provider_usage_contract_violation",
        "response_schema_violation",
    }
)
UNKNOWN_OUTCOME_REASON_CODES = frozenset(
    {
        "connection_reset_after_dispatch",
        "dispatch_crash",
        "provider_timeout_after_dispatch",
        "response_persistence_failed_after_dispatch",
        "lease_expired_after_dispatch",
    }
)
TERMINAL_REPLAY_CONTRACT = (
    "repository_same_operation_same_command_noop_conflicting_replay_reject"
)
_PROHIBITED_WORKER_INGEST_KEYS = frozenset(
    {
        "attachments",
        "attachment_text",
        "attachment_content",
        "raw_payload",
        "raw_request",
        "request_body",
    }
)


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _timestamp(value: object, code: str) -> datetime:
    if isinstance(value, datetime):
        return require_utc(value, code)
    if not isinstance(value, str):
        raise ContractViolation(code)
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), code)
    except ValueError as exc:
        raise ContractViolation(code) from exc


def _worker_authority(
    actor: VerifiedActor, expected_owner_user_id: UUID
) -> UUID:
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_memory_worker")
    if actor.role is not ActorRole.WORKER:
        raise ContractViolation("memory_worker_role_denied")
    owner = _uuid(expected_owner_user_id, "invalid_expected_owner")
    require_owner(actor, owner)
    require_scope(actor, ActorScope.PROCESS_MEMORY_INGEST)
    return owner


def _validated_bridge_source(
    raw: Mapping[str, Any],
    *,
    actor: VerifiedActor,
    expected_owner_user_id: UUID,
) -> VerifiedBridgeSource:
    owner = _worker_authority(actor, expected_owner_user_id)
    if not isinstance(raw, Mapping) or tuple(raw) != _BRIDGE_LEASE_SOURCE_FIELDS:
        raise ContractViolation("invalid_bridge_lease_source")
    row_owner = _uuid(raw["owner_user_id"], "invalid_bridge_lease_owner")
    if row_owner != owner:
        raise ContractViolation("cross_owner_bridge_lease")
    source_created_at = _timestamp(
        raw["source_created_at"], "invalid_bridge_source_created_at"
    )
    ingest_after = _timestamp(raw["ingest_after"], "invalid_bridge_ingest_after")
    if source_created_at < ingest_after:
        raise ContractViolation("bridge_source_before_cutover")
    policy = EligibilityPolicy(ingest_after=ingest_after)
    policy_sha256 = require_sha256(
        raw["policy_sha256"], "invalid_bridge_policy_sha256"
    )
    if policy_sha256 != policy.policy_sha256:
        raise ContractViolation("bridge_policy_sha256_mismatch")
    decision = raw["eligibility_decision"]
    if decision is not None:
        try:
            decision = EligibilityDecision(decision).value
        except (TypeError, ValueError) as exc:
            raise ContractViolation("invalid_bridge_eligibility_decision") from exc
    review_count = require_exact_int(
        raw["context_review_count"],
        code="invalid_bridge_context_review_count",
        maximum=1,
    )
    if review_count == 1 and decision != EligibilityDecision.REVIEW_CONTEXT.value:
        raise ContractViolation("bridge_context_review_state_mismatch")
    source = VerifiedBridgeSource(
        outbox_id=_uuid(raw["outbox_id"], "invalid_bridge_outbox_id"),
        owner_user_id=row_owner,
        message_id=_uuid(raw["message_id"], "invalid_bridge_message_id"),
        thread_id=_uuid(raw["thread_id"], "invalid_bridge_thread_id"),
        exchange_id=_uuid(raw["exchange_id"], "invalid_bridge_exchange_id"),
        window_id=_uuid(raw["window_id"], "invalid_bridge_window_id"),
        window_ordinal=require_exact_int(
            raw["window_ordinal"],
            code="invalid_bridge_window_ordinal",
            maximum=10_000,
        ),
        window_sha256=require_sha256(
            raw["window_sha256"], "invalid_bridge_window_sha256"
        ),
        content_sha256=require_sha256(
            raw["content_sha256"], "invalid_bridge_content_sha256"
        ),
        source_binding_sha256=require_sha256(
            raw["source_binding_sha256"], "invalid_bridge_source_binding_sha256"
        ),
        policy_sha256=policy_sha256,
        source_created_at=source_created_at,
        ingest_after=ingest_after,
        context_review_count=review_count,
        eligibility_decision=decision,
    )
    expected_binding = bridge_source_binding_sha256(
        owner_user_id=source.owner_user_id,
        message_id=source.message_id,
        thread_id=source.thread_id,
        exchange_id=source.exchange_id,
        window_id=source.window_id,
        window_ordinal=source.window_ordinal,
        window_sha256=source.window_sha256,
        content_sha256=source.content_sha256,
        policy_sha256=source.policy_sha256,
        source_created_at=source.source_created_at,
    )
    if source.source_binding_sha256 != expected_binding:
        raise ContractViolation("bridge_source_binding_sha256_mismatch")
    return source


def validate_claimed_bridge_lease(
    raw: Mapping[str, Any],
    *,
    actor: VerifiedActor,
    expected_owner_user_id: UUID,
    transaction_time: datetime,
) -> VerifiedClaimedBridgeLease:
    """Validate one exact row from either active bridge lease function."""

    if not isinstance(raw, Mapping) or tuple(raw) != BRIDGE_LEASE_FIELDS:
        raise ContractViolation("invalid_claimed_bridge_lease")
    source = _validated_bridge_source(
        {key: raw[key] for key in _BRIDGE_LEASE_SOURCE_FIELDS},
        actor=actor,
        expected_owner_user_id=expected_owner_user_id,
    )
    lease_expires_at = _timestamp(
        raw["lease_expires_at"], "invalid_bridge_lease_expiry"
    )
    now = require_utc(transaction_time, "invalid_bridge_transaction_time")
    if lease_expires_at <= now:
        raise ContractViolation("bridge_lease_expired")
    return VerifiedClaimedBridgeLease(
        source=source,
        lease_token=_uuid(raw["lease_token"], "invalid_bridge_lease_token"),
        lease_expires_at=lease_expires_at,
    )


def _require_payload_matches_bridge_source(
    payload: Mapping[str, Any], source: VerifiedBridgeSource
) -> None:
    if not isinstance(payload, Mapping):
        raise ContractViolation("invalid_ingest_payload")
    expected = (
        ("owner_user_id", source.owner_user_id, "invalid_ingest_owner"),
        ("message_id", source.message_id, "invalid_ingest_message"),
        ("thread_id", source.thread_id, "invalid_ingest_thread"),
        ("exchange_id", source.exchange_id, "invalid_ingest_exchange"),
        ("window_id", source.window_id, "invalid_ingest_window"),
    )
    for field, expected_value, code in expected:
        if _uuid(payload.get(field), code) != expected_value:
            raise ContractViolation("bridge_payload_authority_mismatch")
    if require_exact_int(
        payload.get("window_ordinal"),
        code="invalid_ingest_window_ordinal",
        maximum=10_000,
    ) != source.window_ordinal:
        raise ContractViolation("bridge_payload_authority_mismatch")
    if require_sha256(
        payload.get("window_sha256"), "invalid_ingest_window_sha256"
    ) != source.window_sha256:
        raise ContractViolation("bridge_payload_authority_mismatch")
    if require_sha256(
        payload.get("content_sha256"), "invalid_ingest_content_sha256"
    ) != source.content_sha256:
        raise ContractViolation("bridge_payload_authority_mismatch")
    created_at = _timestamp(payload.get("created_at"), "invalid_ingest_timestamp")
    if created_at != source.source_created_at:
        raise ContractViolation("bridge_payload_authority_mismatch")


def _validated_ingest_successor_receipt(
    raw: Mapping[str, Any], source: VerifiedBridgeSource
) -> dict[str, str]:
    expected_fields = INGEST_SUCCESSOR_RECEIPT_FIELDS + ("receipt_sha256",)
    if not isinstance(raw, Mapping) or tuple(raw) != expected_fields:
        raise ContractViolation("invalid_ingest_successor_receipt")
    owner = _uuid(raw["owner_user_id"], "invalid_ingest_receipt_owner")
    operation = _uuid(raw["operation_id"], "invalid_ingest_receipt_operation")
    bridge_binding = require_sha256(
        raw["bridge_source_binding_sha256"],
        "invalid_ingest_receipt_bridge_binding",
    )
    evidence = _uuid(raw["evidence_id"], "invalid_ingest_receipt_evidence")
    job = _uuid(raw["extraction_job_id"], "invalid_ingest_receipt_job")
    decision = raw["decision"]
    if (
        owner != source.owner_user_id
        or operation != source.message_id
        or bridge_binding != source.source_binding_sha256
        or decision != EligibilityDecision.SEND_EXTERNAL.value
    ):
        raise ContractViolation("ingest_successor_receipt_authority_mismatch")
    expected_hash = ingest_successor_receipt_sha256(
        owner_user_id=owner,
        operation_id=operation,
        bridge_source_binding_sha256=bridge_binding,
        decision=decision,
        evidence_id=evidence,
        extraction_job_id=job,
    )
    if require_sha256(
        raw["receipt_sha256"], "invalid_ingest_receipt_sha256"
    ) != expected_hash:
        raise ContractViolation("ingest_successor_receipt_sha256_mismatch")
    return {
        "evidence_id": str(evidence),
        "extraction_job_id": str(job),
        "receipt_sha256": expected_hash,
    }


def process_ingest_item(
    payload: Mapping[str, Any],
    *,
    lease_envelope: Mapping[str, Any],
    actor: VerifiedActor,
    expected_owner_user_id: UUID,
    transaction_time: datetime,
    successor_ingest_receipt: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Return an ordered persistence plan without performing any effect."""

    verified_lease = validate_claimed_bridge_lease(
        lease_envelope,
        actor=actor,
        expected_owner_user_id=expected_owner_user_id,
        transaction_time=transaction_time,
    )
    source = verified_lease.source
    _require_payload_matches_bridge_source(payload, source)
    if any(key in payload for key in _PROHIBITED_WORKER_INGEST_KEYS):
        raise ContractViolation("attachment_or_raw_payload_prohibited")
    review_count = source.context_review_count
    if review_count == 0 and source.eligibility_decision is not None:
        raise ContractViolation("bridge_eligibility_replay_mismatch")
    if successor_ingest_receipt is not None and review_count != 1:
        raise ContractViolation("unexpected_ingest_successor_receipt")
    replay_receipt = (
        _validated_ingest_successor_receipt(successor_ingest_receipt, source)
        if successor_ingest_receipt is not None
        else None
    )
    if review_count == 1:
        receipt = {
            "owner_user_id_sha256": canonical_sha256(
                "governed_memory.owner", str(source.owner_user_id)
            ),
            "message_id": str(source.message_id),
            "source_binding_sha256": source.source_binding_sha256,
            "policy_sha256": source.policy_sha256,
            "external_model_calls": 0,
        }
        if replay_receipt is not None:
            return {
                "decision": EligibilityDecision.SEND_EXTERNAL.value,
                "reason_codes": [EligibilityReason.CONTEXT_REQUIRED.value],
                "bridge_state": "completed",
                "selected_evidence": None,
                "provider_allowed": False,
                "effects": ["ack_existing_memory_ingest"],
                "receipt": receipt,
                "context_review_count": 1,
                "resolution": "successor_receipt_replayed",
                "successor_evidence_id": replay_receipt["evidence_id"],
                "successor_job_id": replay_receipt["extraction_job_id"],
                "ingest_receipt_sha256": replay_receipt["receipt_sha256"],
            }
        return {
            "decision": EligibilityDecision.REVIEW_CONTEXT.value,
            "reason_codes": [EligibilityReason.CONTEXT_REQUIRED.value],
            "bridge_state": "completed",
            "selected_evidence": None,
            "provider_allowed": False,
            "effects": ["mark_memory_ingest_context_review"],
            "receipt": receipt,
            "context_review_count": 1,
            "resolution": "unresolved_terminal",
        }
    result = evaluate_eligibility(payload, policy=source.policy)
    if (
        result["source_binding_sha256"] != source.source_binding_sha256
        or result["policy_sha256"] != source.policy_sha256
    ):
        raise ContractViolation("bridge_eligibility_binding_mismatch")
    reasons = tuple(result["reason_codes"])
    if "before_cutover" in reasons:
        bridge_state = "expired"
        effects = ["complete_bridge"]
        resolution = None
        next_review_count = review_count
    elif (
        result["decision"] == EligibilityDecision.REVIEW_CONTEXT.value
        and reasons == (EligibilityReason.CONTEXT_REQUIRED.value,)
        and review_count == 0
    ):
        bridge_state = "claimed"
        effects = ["mark_memory_ingest_context_review", "load_bounded_context"]
        resolution = "context_required"
        next_review_count = 1
    elif result["decision"] == EligibilityDecision.REVIEW_CONTEXT.value:
        bridge_state = "completed"
        effects = ["complete_bridge"]
        resolution = "unresolved_terminal"
        next_review_count = review_count
    elif bool(result["provider_allowed"]):
        bridge_state = "completed"
        effects = [
            "record_selected_evidence",
            "create_extraction_job",
            "complete_bridge",
        ]
        resolution = None
        next_review_count = review_count
    else:
        bridge_state = "completed"
        effects = ["complete_bridge"]
        resolution = None
        next_review_count = review_count
    return {
        "decision": result["decision"],
        "reason_codes": list(reasons),
        "bridge_state": bridge_state,
        "selected_evidence": result["selected_evidence"],
        "provider_allowed": result["provider_allowed"],
        "effects": effects,
        "receipt": result["receipt"],
        "context_review_count": next_review_count,
        "resolution": resolution,
    }


_PERSONAL_QUESTION_RE = re.compile(
    r"\b(?:you|your)\b.*\b(?:prefer|favorite|family|sister|brother|pet|dog|cat|"
    r"live|lived|work|job|believe|think)\b.*\?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SHORT_CONTEXT_ANSWER_RE = re.compile(
    r"^\s*(?:yes|no|actually|still|not|[A-Za-z0-9]).{0,255}$",
    re.DOTALL,
)
_CONTEXT_FIELDS = {
    "context_message_id",
    "owner_user_id",
    "thread_id",
    "exchange_id",
    "window_id",
    "window_sha256",
    "context_created_at",
    "context_ordinal",
    "source_ordinal",
    "role",
    "content",
    "content_sha256",
    "distance",
    "review_count",
    "exchange_attachment_count",
}


def resolve_context_review(
    payload: Mapping[str, Any],
    bounded_context: Mapping[str, Any],
    *,
    lease_envelope: Mapping[str, Any],
    actor: VerifiedActor,
    expected_owner_user_id: UUID,
    transaction_time: datetime,
) -> dict[str, object]:
    verified_lease = validate_claimed_bridge_lease(
        lease_envelope,
        actor=actor,
        expected_owner_user_id=expected_owner_user_id,
        transaction_time=transaction_time,
    )
    bridge_source = verified_lease.source
    _require_payload_matches_bridge_source(payload, bridge_source)
    owner = bridge_source.owner_user_id
    source, initial = classify_eligibility(payload, policy=bridge_source.policy)
    if (
        initial.source_binding_sha256 != bridge_source.source_binding_sha256
        or initial.policy_sha256 != bridge_source.policy_sha256
    ):
        raise ContractViolation("bridge_context_binding_mismatch")
    authoritative_review_count = bridge_source.context_review_count
    if (
        authoritative_review_count != 1
        or bridge_source.eligibility_decision
        != EligibilityDecision.REVIEW_CONTEXT.value
        or
        initial.decision is not EligibilityDecision.REVIEW_CONTEXT
        or initial.reason_codes != (EligibilityReason.CONTEXT_REQUIRED,)
    ):
        raise ContractViolation("context_review_not_required")
    if not isinstance(bounded_context, Mapping) or set(bounded_context) != _CONTEXT_FIELDS:
        raise ContractViolation("invalid_bounded_context")
    context_owner = _uuid(
        bounded_context["owner_user_id"], "invalid_context_owner"
    )
    context_message_id = _uuid(
        bounded_context["context_message_id"], "invalid_context_message_id"
    )
    context_thread = _uuid(
        bounded_context["thread_id"], "invalid_context_thread"
    )
    context_exchange = _uuid(
        bounded_context["exchange_id"], "invalid_context_exchange"
    )
    context_window = _uuid(
        bounded_context["window_id"], "invalid_context_window"
    )
    context_window_sha256 = require_sha256(
        bounded_context["window_sha256"], "invalid_context_window_sha256"
    )
    if (
        context_owner != owner
        or context_thread != source.thread_id
        or context_exchange != source.exchange_id
        or context_window != source.window_id
        or context_window_sha256 != source.window_sha256
    ):
        raise ContractViolation("context_source_authority_mismatch")
    if context_message_id == source.message_id:
        raise ContractViolation("context_source_message_collision")
    if bounded_context["role"] != "assistant":
        raise ContractViolation("invalid_context_role")
    distance = require_exact_int(
        bounded_context["distance"], code="invalid_context_distance", minimum=1, maximum=1
    )
    review_count = require_exact_int(
        bounded_context["review_count"], code="invalid_context_review_count", minimum=1, maximum=1
    )
    if review_count != authoritative_review_count:
        raise ContractViolation("context_review_count_mismatch")
    exchange_attachment_count = require_exact_int(
        bounded_context["exchange_attachment_count"],
        code="invalid_context_exchange_attachment_count",
        maximum=10_000,
    )
    context_ordinal = require_exact_int(
        bounded_context["context_ordinal"], code="invalid_context_ordinal"
    )
    source_ordinal = require_exact_int(
        bounded_context["source_ordinal"], code="invalid_source_ordinal", minimum=1
    )
    if source_ordinal != source.window_ordinal:
        raise ContractViolation("context_source_ordinal_mismatch")
    if context_ordinal + 1 != source_ordinal:
        raise ContractViolation("context_not_immediately_preceding")
    context_created_at = _timestamp(
        bounded_context["context_created_at"], "invalid_context_created_at"
    )
    cutover_at = bridge_source.ingest_after
    if context_created_at < cutover_at:
        raise ContractViolation("context_before_cutover")
    if context_created_at > source.created_at:
        raise ContractViolation("context_after_source")
    context_text = require_bounded_text(
        bounded_context["content"],
        code="invalid_context_content",
        maximum_bytes=2_000,
    )
    context_sha256 = require_sha256(
        bounded_context["content_sha256"], "invalid_context_content_sha256"
    )
    if sha256_text(context_text) != context_sha256:
        raise ContractViolation("context_content_sha256_mismatch")
    lineage_sha256 = canonical_sha256(
        "governed_memory.context_review_lineage",
        {
            "owner_user_id": owner,
            "thread_id": source.thread_id,
            "source_message_id": source.message_id,
            "exchange_id": source.exchange_id,
            "source_window_id": source.window_id,
            "window_sha256": source.window_sha256,
            "context_message_id": context_message_id,
            "context_sha256": context_sha256,
            "context_created_at": context_created_at,
            "context_ordinal": context_ordinal,
            "source_ordinal": source_ordinal,
            "distance": distance,
            "review_count": review_count,
            "exchange_attachment_count": exchange_attachment_count,
        },
    )
    resolvable = bool(
        _PERSONAL_QUESTION_RE.search(context_text)
        and _SHORT_CONTEXT_ANSWER_RE.fullmatch(source.content)
        and "?" not in source.content
    )
    receipt_base = {
        "owner_user_id_sha256": canonical_sha256(
            "governed_memory.owner", str(owner)
        ),
        "message_id": str(source.message_id),
        "thread_id": str(source.thread_id),
        "exchange_id": str(source.exchange_id),
        "window_id": str(source.window_id),
        "window_sha256": source.window_sha256,
        "context_message_id": str(context_message_id),
        "context_sha256": context_sha256,
        "context_review_count": review_count,
        "lineage_sha256": lineage_sha256,
        "external_model_calls": 0,
    }
    if exchange_attachment_count != 0:
        return {
            "decision": EligibilityDecision.BLOCK_LOCAL.value,
            "reason_codes": [EligibilityReason.ATTACHMENT_NOT_AUTHORIZED.value],
            "resolution": "unresolved_terminal",
            "bridge_state": "completed",
            "selected_evidence": None,
            "provider_context": None,
            "provider_allowed": False,
            "effects": ["complete_bridge"],
            "receipt": {
                **receipt_base,
                "decision": EligibilityDecision.BLOCK_LOCAL.value,
                "reason_codes": [
                    EligibilityReason.ATTACHMENT_NOT_AUTHORIZED.value
                ],
                "resolution": "unresolved_terminal",
            },
            "context_review_count": review_count,
        }
    context_privacy_denial = external_send_privacy_denial(context_text)
    if context_privacy_denial is not None:
        denial_decision = (
            EligibilityDecision.ROUTE_INTERNAL
            if context_privacy_denial is EligibilityReason.SENSITIVE_LOCAL_ONLY
            else EligibilityDecision.BLOCK_LOCAL
        )
        return {
            "decision": denial_decision.value,
            "reason_codes": [context_privacy_denial.value],
            "resolution": "unresolved_terminal",
            "bridge_state": "completed",
            "selected_evidence": None,
            "provider_context": None,
            "provider_allowed": False,
            "effects": ["complete_bridge"],
            "receipt": {
                **receipt_base,
                "decision": denial_decision.value,
                "reason_codes": [context_privacy_denial.value],
                "resolution": "unresolved_terminal",
            },
            "context_review_count": review_count,
        }
    context_route_denial = context_external_route_denial(context_text)
    if context_route_denial is not None:
        return {
            "decision": EligibilityDecision.ROUTE_INTERNAL.value,
            "reason_codes": [context_route_denial.value],
            "resolution": "unresolved_terminal",
            "bridge_state": "completed",
            "selected_evidence": None,
            "provider_context": None,
            "provider_allowed": False,
            "effects": ["complete_bridge"],
            "receipt": {
                **receipt_base,
                "decision": EligibilityDecision.ROUTE_INTERNAL.value,
                "reason_codes": [context_route_denial.value],
                "resolution": "unresolved_terminal",
            },
            "context_review_count": review_count,
        }
    if not resolvable:
        return {
            "decision": EligibilityDecision.REVIEW_CONTEXT.value,
            "resolution": "unresolved_terminal",
            "bridge_state": "completed",
            "selected_evidence": None,
            "provider_context": None,
            "provider_allowed": False,
            "effects": ["mark_memory_ingest_context_review"],
            "receipt": {
                **receipt_base,
                "decision": EligibilityDecision.REVIEW_CONTEXT.value,
                "resolution": "unresolved_terminal",
            },
            "context_review_count": review_count,
        }
    selected_text = source.content
    selected_sha256 = sha256_text(selected_text)
    selection = selection_binding_sha256(
        owner_user_id=owner,
        source_kind="conversation_message",
        source_message_id=source.message_id,
        source_thread_id=source.thread_id,
        source_window_id=source.window_id,
        window_sha256=source.window_sha256,
        source_sha256=source.content_sha256,
        selected_sha256=selected_sha256,
        start_utf8=0,
        end_utf8=len(selected_text.encode("utf-8")),
        context_message_id=context_message_id,
        context_sha256=context_sha256,
    )
    selected = {
        "owner_user_id": str(owner),
        "source_kind": "conversation_message",
        "source_message_id": str(source.message_id),
        "source_thread_id": str(source.thread_id),
        "source_window_id": str(source.window_id),
        "start_utf8": 0,
        "end_utf8": len(selected_text.encode("utf-8")),
        "selected_text": selected_text,
        "selected_sha256": selected_sha256,
        "source_sha256": source.content_sha256,
        "window_sha256": source.window_sha256,
        "context_message_id": str(context_message_id),
        "context_sha256": context_sha256,
        "selection_binding_sha256": selection,
        "category": "context_bound_personal_fact",
        "assertion_mode": "asserted",
        "subject_hint": "owner",
        "sensitivity": "ordinary",
    }
    return {
        "decision": EligibilityDecision.SEND_EXTERNAL.value,
        "resolution": "context_resolved",
        "bridge_state": "completed",
        "selected_evidence": selected,
        "provider_context": {
            "role": "assistant",
            "content": context_text,
            "content_sha256": context_sha256,
        },
        "provider_allowed": True,
        "effects": [
            "record_selected_evidence",
            "create_extraction_job",
            "complete_bridge",
        ],
        "receipt": {
            **receipt_base,
            "source_sha256": source.content_sha256,
            "selected_sha256": selected_sha256,
            "selection_binding_sha256": selection,
            "decision": EligibilityDecision.SEND_EXTERNAL.value,
            "resolution": "context_resolved",
        },
        "context_review_count": review_count,
    }


_JOB_FIELDS = {
    "job_id",
    "owner_user_id",
    "state",
    "attempts",
    "lease_token",
    "lease_expires_at",
    "active_provider_call_id",
}
_CALL_FIELDS = {
    "provider_call_id",
    "job_id",
    "owner_user_id",
    "attempt_number",
    "state",
    "lease_token",
    "lease_expires_at",
    "request_sha256",
    "receipt_sha256",
    "failure_reason_code",
}


def _validate_job(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _JOB_FIELDS:
        raise ContractViolation("invalid_provider_job_record")
    row = dict(value)
    row["job_id"] = str(_uuid(row["job_id"], "invalid_provider_job_id"))
    row["owner_user_id"] = str(
        _uuid(row["owner_user_id"], "invalid_provider_job_owner")
    )
    try:
        row["state"] = ExtractionJobState(row["state"]).value
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_extraction_job_state") from exc
    row["attempts"] = require_exact_int(
        row["attempts"], code="invalid_extraction_attempts", maximum=100
    )
    if (row["lease_token"] is None) != (row["lease_expires_at"] is None):
        raise ContractViolation("incomplete_job_lease")
    if row["lease_token"] is not None:
        row["lease_token"] = str(
            _uuid(row["lease_token"], "invalid_job_lease_token")
        )
        row["lease_expires_at"] = _timestamp(
            row["lease_expires_at"], "invalid_job_lease_expiry"
        )
    if row["active_provider_call_id"] is not None:
        row["active_provider_call_id"] = str(
            _uuid(
                row["active_provider_call_id"], "invalid_active_provider_call_id"
            )
        )
    claimed = row["state"] == ExtractionJobState.CLAIMED.value
    if claimed != (
        row["lease_token"] is not None
        and row["lease_expires_at"] is not None
        and row["active_provider_call_id"] is not None
    ):
        raise ContractViolation("incoherent_provider_job_lease")
    if not claimed and any(
        row[field] is not None
        for field in ("lease_token", "lease_expires_at", "active_provider_call_id")
    ):
        raise ContractViolation("incoherent_provider_job_lease")
    if row["state"] == ExtractionJobState.PENDING.value and row["attempts"] != 0:
        raise ContractViolation("incoherent_pending_provider_job")
    if row["state"] == ExtractionJobState.RETRYABLE.value and row["attempts"] < 1:
        raise ContractViolation("incoherent_retryable_provider_job")
    if claimed and row["attempts"] < 1:
        raise ContractViolation("incoherent_claimed_provider_job")
    return row


def _validate_call(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _CALL_FIELDS:
        raise ContractViolation("invalid_provider_call_record")
    row = dict(value)
    for field, code in (
        ("provider_call_id", "invalid_provider_call_id"),
        ("job_id", "invalid_provider_call_job_id"),
        ("owner_user_id", "invalid_provider_call_owner"),
        ("lease_token", "invalid_provider_call_lease_token"),
    ):
        row[field] = str(_uuid(row[field], code))
    row["attempt_number"] = require_exact_int(
        row["attempt_number"], code="invalid_provider_attempt_number", minimum=1, maximum=100
    )
    try:
        row["state"] = ProviderCallState(row["state"]).value
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_provider_call_state") from exc
    row["lease_expires_at"] = _timestamp(
        row["lease_expires_at"], "invalid_provider_call_lease_expiry"
    )
    if row["request_sha256"] is not None:
        row["request_sha256"] = require_sha256(
            row["request_sha256"], "invalid_provider_request_sha256"
        )
    if row["receipt_sha256"] is not None:
        row["receipt_sha256"] = require_sha256(
            row["receipt_sha256"], "invalid_provider_receipt_sha256"
        )
    if row["failure_reason_code"] is not None and not isinstance(
        row["failure_reason_code"], str
    ):
        raise ContractViolation("invalid_provider_failure_reason")
    state = ProviderCallState(row["state"])
    reason = row["failure_reason_code"]
    if state is ProviderCallState.RESERVED and (
        row["request_sha256"] is not None
        or row["receipt_sha256"] is not None
        or reason is not None
    ):
        raise ContractViolation("incoherent_reserved_provider_call")
    if state is ProviderCallState.DISPATCHED and (
        row["request_sha256"] is None
        or row["receipt_sha256"] is not None
        or reason is not None
    ):
        raise ContractViolation("incoherent_dispatched_provider_call")
    if state is ProviderCallState.COMPLETED and (
        row["request_sha256"] is None
        or row["receipt_sha256"] is None
        or reason is not None
    ):
        raise ContractViolation("incoherent_completed_provider_call")
    if state is ProviderCallState.RETRYABLE_FAILURE and reason not in NOT_EXECUTED_REASON_CODES:
        raise ContractViolation("invalid_not_executed_reason_code")
    if state is ProviderCallState.RETRYABLE_FAILURE and (
        row["request_sha256"] is not None or row["receipt_sha256"] is not None
    ):
        raise ContractViolation("incoherent_retryable_provider_call")
    if state is ProviderCallState.TERMINAL_FAILURE and reason not in TERMINAL_PROVIDER_REASON_CODES:
        raise ContractViolation("invalid_terminal_provider_reason_code")
    if state is ProviderCallState.TERMINAL_FAILURE and (
        row["request_sha256"] is None or row["receipt_sha256"] is not None
    ):
        raise ContractViolation("incoherent_terminal_provider_call")
    if state is ProviderCallState.OUTCOME_UNKNOWN and reason not in UNKNOWN_OUTCOME_REASON_CODES:
        raise ContractViolation("invalid_unknown_outcome_reason_code")
    if state is ProviderCallState.OUTCOME_UNKNOWN and (
        row["request_sha256"] is None or row["receipt_sha256"] is not None
    ):
        raise ContractViolation("incoherent_unknown_provider_call")
    return row


def _retry_job(job: dict[str, Any], limit: int) -> dict[str, Any]:
    exhausted = job["attempts"] >= limit
    return {
        **job,
        "state": (
            ExtractionJobState.FAILED_TERMINAL.value
            if exhausted
            else ExtractionJobState.RETRYABLE.value
        ),
        "lease_token": None,
        "lease_expires_at": None,
        "active_provider_call_id": None,
    }


def transition_provider_attempt(
    job_record: Mapping[str, Any],
    provider_call_record: Mapping[str, Any] | None,
    command: Mapping[str, Any],
    *,
    max_attempts: int,
) -> dict[str, object]:
    job = _validate_job(job_record)
    call = _validate_call(provider_call_record)
    if not isinstance(command, Mapping) or "action" not in command:
        raise ContractViolation("invalid_provider_attempt_command")
    limit = require_exact_int(
        max_attempts,
        code="invalid_extraction_attempt_limit",
        minimum=1,
        maximum=100,
    )
    if job["attempts"] > limit:
        raise ContractViolation("extraction_attempts_exceed_limit")
    try:
        action = ProviderAttemptAction(command["action"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_provider_attempt_action") from exc
    now = _timestamp(command.get("now"), "invalid_provider_attempt_timestamp")
    if job["state"] in {
        ExtractionJobState.COMPLETED.value,
        ExtractionJobState.FAILED_TERMINAL.value,
    }:
        raise ContractViolation("terminal_provider_job_reentry")

    if action is ProviderAttemptAction.RESERVE:
        if set(command) != {
            "action",
            "now",
            "lease_token",
            "lease_expires_at",
            "provider_call_id",
        }:
            raise ContractViolation("invalid_provider_reserve_command")
        if job["state"] not in {
            ExtractionJobState.PENDING.value,
            ExtractionJobState.RETRYABLE.value,
        }:
            raise ContractViolation("provider_job_not_reservable")
        if job["state"] == ExtractionJobState.PENDING.value and call is not None:
            raise ContractViolation("pending_job_has_prior_provider_call")
        if job["state"] == ExtractionJobState.RETRYABLE.value and (
            call is None
            or call["state"] != ProviderCallState.RETRYABLE_FAILURE.value
            or call["job_id"] != job["job_id"]
            or call["owner_user_id"] != job["owner_user_id"]
            or call["attempt_number"] != job["attempts"]
        ):
            raise ContractViolation("retryable_job_prior_call_mismatch")
        if job["attempts"] >= limit:
            next_job = {
                **job,
                "state": ExtractionJobState.FAILED_TERMINAL.value,
                "lease_token": None,
                "lease_expires_at": None,
                "active_provider_call_id": None,
            }
            return {
                "job_record": next_job,
                "provider_call_record": call,
                "effects": ["record_attempt_limit_terminal"],
                "call_allowed": False,
                "terminal": True,
            }
        lease_token = _uuid(command["lease_token"], "invalid_new_lease_token")
        lease_expiry = _timestamp(
            command["lease_expires_at"], "invalid_new_lease_expiry"
        )
        if lease_expiry <= now:
            raise ContractViolation("new_lease_not_future")
        provider_call_id = _uuid(
            command["provider_call_id"], "invalid_new_provider_call_id"
        )
        if call is not None and str(provider_call_id) == call["provider_call_id"]:
            raise ContractViolation("provider_call_id_reuse")
        attempt = job["attempts"] + 1
        next_job = {
            **job,
            "state": ExtractionJobState.CLAIMED.value,
            "attempts": attempt,
            "lease_token": str(lease_token),
            "lease_expires_at": lease_expiry,
            "active_provider_call_id": str(provider_call_id),
        }
        next_call = {
            "provider_call_id": str(provider_call_id),
            "job_id": job["job_id"],
            "owner_user_id": job["owner_user_id"],
            "attempt_number": attempt,
            "state": ProviderCallState.RESERVED.value,
            "lease_token": str(lease_token),
            "lease_expires_at": lease_expiry,
            "request_sha256": None,
            "receipt_sha256": None,
            "failure_reason_code": None,
        }
        return {
            "job_record": next_job,
            "provider_call_record": next_call,
            "effects": ["persist_provider_reservation"],
            "call_allowed": False,
            "terminal": False,
        }

    if call is None:
        raise ContractViolation("provider_call_record_required")
    if (
        job["state"] != ExtractionJobState.CLAIMED.value
        or job["active_provider_call_id"] != call["provider_call_id"]
        or job["job_id"] != call["job_id"]
        or job["owner_user_id"] != call["owner_user_id"]
        or job["lease_token"] != call["lease_token"]
        or job["lease_expires_at"] != call["lease_expires_at"]
        or job["attempts"] != call["attempt_number"]
    ):
        raise ContractViolation("provider_job_call_binding_mismatch")
    if action is not ProviderAttemptAction.LEASE_EXPIRED:
        expected_keys = {"action", "now", "lease_token"}
        if action is ProviderAttemptAction.MARK_DISPATCHED:
            expected_keys.add("request_sha256")
        elif action is ProviderAttemptAction.COMPLETE:
            expected_keys.add("provider_receipt")
        elif action in {
            ProviderAttemptAction.FAIL_NOT_EXECUTED,
            ProviderAttemptAction.FAIL_TERMINAL,
            ProviderAttemptAction.FAIL_UNKNOWN,
        }:
            expected_keys.add("reason_code")
        if set(command) != expected_keys:
            raise ContractViolation("invalid_provider_active_command")
        supplied_lease = str(
            _uuid(command["lease_token"], "invalid_supplied_lease_token")
        )
        if supplied_lease != call["lease_token"]:
            raise ContractViolation("stale_provider_lease_token")
        if now >= call["lease_expires_at"]:
            raise ContractViolation("provider_lease_expired")
    else:
        if set(command) != {"action", "now", "lease_token"}:
            raise ContractViolation("invalid_provider_lease_expired_command")
        supplied_lease = str(
            _uuid(command["lease_token"], "invalid_supplied_lease_token")
        )
        if supplied_lease != call["lease_token"]:
            raise ContractViolation("stale_provider_lease_token")
        if now < call["lease_expires_at"]:
            raise ContractViolation("provider_lease_not_expired")

    if action is ProviderAttemptAction.MARK_DISPATCHED:
        if call["state"] != ProviderCallState.RESERVED.value:
            raise ContractViolation("provider_call_not_reserved")
        request_sha256 = require_sha256(
            command["request_sha256"], "invalid_provider_request_sha256"
        )
        return {
            "job_record": job,
            "provider_call_record": {
                **call,
                "state": ProviderCallState.DISPATCHED.value,
                "request_sha256": request_sha256,
            },
            "effects": ["persist_provider_dispatch_before_external_call"],
            "call_allowed": True,
            "terminal": False,
        }
    if action is ProviderAttemptAction.COMPLETE:
        if call["state"] != ProviderCallState.DISPATCHED.value:
            raise ContractViolation("provider_call_not_dispatched")
        receipt = command["provider_receipt"]
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "provider_call_id",
            "request_sha256",
            "receipt_sha256",
        }:
            raise ContractViolation("invalid_exact_provider_receipt")
        if (
            str(_uuid(receipt["provider_call_id"], "invalid_receipt_provider_call"))
            != call["provider_call_id"]
            or require_sha256(
                receipt["request_sha256"], "invalid_receipt_request_sha256"
            )
            != call["request_sha256"]
        ):
            raise ContractViolation("provider_receipt_binding_mismatch")
        receipt_sha256 = require_sha256(
            receipt["receipt_sha256"], "invalid_provider_receipt_sha256"
        )
        return {
            "job_record": {
                **job,
                "state": ExtractionJobState.COMPLETED.value,
                "lease_token": None,
                "lease_expires_at": None,
                "active_provider_call_id": None,
            },
            "provider_call_record": {
                **call,
                "state": ProviderCallState.COMPLETED.value,
                "receipt_sha256": receipt_sha256,
            },
            "effects": ["persist_provider_completion", "complete_extraction_job"],
            "call_allowed": False,
            "terminal": True,
        }
    if action is ProviderAttemptAction.FAIL_NOT_EXECUTED:
        if call["state"] != ProviderCallState.RESERVED.value:
            raise ContractViolation("provider_call_not_failable")
        reason = command["reason_code"]
        if reason not in NOT_EXECUTED_REASON_CODES:
            raise ContractViolation("invalid_not_executed_reason_code")
        next_job = _retry_job(job, limit)
        return {
            "job_record": next_job,
            "provider_call_record": {
                **call,
                "state": ProviderCallState.RETRYABLE_FAILURE.value,
                "failure_reason_code": reason,
            },
            "effects": [
                "persist_proved_not_executed_failure",
                "schedule_retry"
                if next_job["state"] == ExtractionJobState.RETRYABLE.value
                else "record_attempt_limit_terminal",
            ],
            "call_allowed": False,
            "terminal": next_job["state"] == ExtractionJobState.FAILED_TERMINAL.value,
        }
    if action is ProviderAttemptAction.FAIL_TERMINAL:
        if call["state"] != ProviderCallState.DISPATCHED.value:
            raise ContractViolation("provider_call_not_failable")
        reason = command["reason_code"]
        if reason not in TERMINAL_PROVIDER_REASON_CODES:
            raise ContractViolation("invalid_terminal_provider_reason_code")
        return {
            "job_record": {
                **job,
                "state": ExtractionJobState.FAILED_TERMINAL.value,
                "lease_token": None,
                "lease_expires_at": None,
                "active_provider_call_id": None,
            },
            "provider_call_record": {
                **call,
                "state": ProviderCallState.TERMINAL_FAILURE.value,
                "failure_reason_code": reason,
            },
            "effects": ["persist_terminal_provider_failure"],
            "call_allowed": False,
            "terminal": True,
        }
    if action is ProviderAttemptAction.FAIL_UNKNOWN:
        if call["state"] != ProviderCallState.DISPATCHED.value:
            raise ContractViolation("unknown_outcome_requires_dispatch")
        reason = command["reason_code"]
        if reason not in UNKNOWN_OUTCOME_REASON_CODES:
            raise ContractViolation("invalid_unknown_outcome_reason_code")
        next_call = {
            **call,
            "state": ProviderCallState.OUTCOME_UNKNOWN.value,
            "failure_reason_code": reason,
        }
        next_job = {
            **job,
            "state": ExtractionJobState.FAILED_TERMINAL.value,
            "lease_token": None,
            "lease_expires_at": None,
            "active_provider_call_id": None,
        }
        return {
            "job_record": next_job,
            "provider_call_record": next_call,
            "effects": ["persist_outcome_unknown_terminal"],
            "call_allowed": False,
            "terminal": True,
        }
    if action is ProviderAttemptAction.LEASE_EXPIRED:
        if call["state"] == ProviderCallState.RESERVED.value:
            next_job = _retry_job(job, limit)
            next_call = {
                **call,
                "state": ProviderCallState.RETRYABLE_FAILURE.value,
                "failure_reason_code": "lease_expired_before_dispatch",
            }
            return {
                "job_record": next_job,
                "provider_call_record": next_call,
                "effects": [
                    "persist_reserved_lease_expiry",
                    "schedule_retry"
                    if next_job["state"] == ExtractionJobState.RETRYABLE.value
                    else "record_attempt_limit_terminal",
                ],
                "call_allowed": False,
                "terminal": next_job["state"] == ExtractionJobState.FAILED_TERMINAL.value,
            }
        if call["state"] == ProviderCallState.DISPATCHED.value:
            return {
                "job_record": {
                    **job,
                    "state": ExtractionJobState.FAILED_TERMINAL.value,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "active_provider_call_id": None,
                },
                "provider_call_record": {
                    **call,
                    "state": ProviderCallState.OUTCOME_UNKNOWN.value,
                    "failure_reason_code": "lease_expired_after_dispatch",
                },
                "effects": ["persist_outcome_unknown_terminal"],
                "call_allowed": False,
                "terminal": True,
            }
        raise ContractViolation("provider_call_not_lease_expirable")
    raise ContractViolation("invalid_provider_attempt_action")


__all__ = [
    "BRIDGE_LEASE_FIELDS",
    "INGEST_SUCCESSOR_RECEIPT_DOMAIN",
    "INGEST_SUCCESSOR_RECEIPT_FIELDS",
    "NOT_EXECUTED_REASON_CODES",
    "ProviderAttemptAction",
    "ProviderCallState",
    "TERMINAL_PROVIDER_REASON_CODES",
    "TERMINAL_REPLAY_CONTRACT",
    "UNKNOWN_OUTCOME_REASON_CODES",
    "VerifiedBridgeSource",
    "VerifiedClaimedBridgeLease",
    "ingest_successor_receipt_sha256",
    "process_ingest_item",
    "resolve_context_review",
    "transition_provider_attempt",
    "validate_claimed_bridge_lease",
]
