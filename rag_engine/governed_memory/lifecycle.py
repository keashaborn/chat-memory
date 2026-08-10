from __future__ import annotations

"""Pure compare-and-swap claim lifecycle planning."""

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .admission import (
    claim_identity_sha256,
    recompute_claim_state_sha256,
    relational_fact_from_row,
)
from .auth import ActorRole, ActorScope, VerifiedActor, require_owner, require_scope
from .contracts import (
    ClaimLifecycleState,
    ContractViolation,
    EntityKind,
    ObjectKind,
    OperationOutcome,
    ProposalState,
    canonical_json_bytes,
    canonical_sha256,
    framed_sha256,
    render_relational_fact,
    require_exact_int,
    require_key,
    require_sha256,
    require_utc,
    selection_binding_sha256,
    semantic_key_sha256,
    sha256_bytes,
    sha256_text,
)
from .extraction import (
    ProposalPurpose,
    derive_source_local_entity_key,
    normalize_catalog_fact,
    parse_predicate_catalog,
    policy_for_sensitivity,
    recompute_proposal_sha256,
)
from .projection import (
    PROJECTION_CONTRACT_SHA256,
    PROJECTION_DELETE_RECEIPT_FIELDS,
    PROJECTION_OUTBOX_FIELDS,
    projection_manifest_sha256,
)


class LifecycleAction(str, Enum):
    CORRECT = "correct"
    RETRACT = "retract"
    REQUEST_DELETE = "request_delete"
    FINALIZE_DELETE = "finalize_delete"


CORRECTION_PROPOSAL_TTL = timedelta(hours=24)


_CLAIM_REQUIRED_FIELDS = {
    "owner_user_id",
    "claim_id",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "projection_sequence",
    "lifecycle_state",
    "is_current",
    "projectable",
    "predicate_catalog_sha256",
    "predicate",
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "object_kind",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
    "epistemic_state",
    "sensitivity",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "selection_binding_sha256",
    "source_sha256",
    "valid_from",
    "valid_to",
    "updated_at",
    "retrieval_text",
    "retrieval_text_sha256",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "state_sha256",
}


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _derived_uuid(operation_id: UUID, purpose: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"governed-memory:{operation_id}:{purpose}")


def _validate_claim(
    claim: Mapping[str, Any], predicate_catalog: Mapping[str, Any]
) -> tuple[UUID, UUID, UUID, ClaimLifecycleState]:
    if not isinstance(claim, Mapping) or not _CLAIM_REQUIRED_FIELDS.issubset(claim):
        raise ContractViolation("lifecycle_claim_missing_field")
    catalog = parse_predicate_catalog(predicate_catalog)
    owner = _uuid(claim["owner_user_id"], "invalid_lifecycle_owner")
    claim_id = _uuid(claim["claim_id"], "invalid_lifecycle_claim_id")
    revision_id = _uuid(claim["revision_id"], "invalid_lifecycle_revision_id")
    require_exact_int(
        claim["revision_number"],
        code="invalid_claim_revision_number",
        minimum=1,
    )
    require_exact_int(
        claim["projection_sequence"],
        code="invalid_claim_projection_sequence",
    )
    require_sha256(claim["revision_sha256"], "invalid_claim_revision_sha256")
    if claim["predicate_catalog_sha256"] != catalog.catalog_sha256:
        raise ContractViolation("claim_predicate_catalog_mismatch")
    try:
        state = ClaimLifecycleState(claim["lifecycle_state"])
        subject_type = EntityKind(claim["subject_entity_type"])
        object_kind = ObjectKind(claim["object_kind"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_claim_relational_state") from exc
    rule = catalog.rule_for(str(claim["predicate"]))
    from .contracts import EpistemicStatus, Sensitivity

    epistemic = EpistemicStatus(claim["epistemic_state"])
    sensitivity = Sensitivity(claim["sensitivity"])
    if (
        subject_type not in rule.subject_kinds
        or object_kind not in rule.object_kinds
        or epistemic not in rule.epistemic_statuses
        or sensitivity not in rule.sensitivities
    ):
        raise ContractViolation("claim_catalog_fact_mismatch")
    expected_policy = policy_for_sensitivity(sensitivity.value)
    if (
        claim["projectable"] is not True
        or claim["domains"] != []
        or claim["intents"] != []
        or claim["surface"] != expected_policy["surface"]
        or claim["requires_explicit"] is not expected_policy["requires_explicit"]
        or claim["valid_from"] is not None
        or claim["valid_to"] is not None
    ):
        raise ContractViolation("claim_policy_mismatch")
    expected_semantic = semantic_key_sha256(
        subject_entity_key=str(claim["subject_entity_key"]),
        predicate=str(claim["predicate"]),
        object_kind=object_kind,
        object_entity_key=claim["object_entity_key"],
        object_literal=claim["object_literal"],
    )
    if claim["semantic_key_sha256"] != expected_semantic:
        raise ContractViolation("claim_semantic_key_mismatch")
    expected_identity = claim_identity_sha256(
        subject_entity_key=str(claim["subject_entity_key"]),
        predicate=str(claim["predicate"]),
    )
    if claim["claim_identity_sha256"] != expected_identity:
        raise ContractViolation("claim_identity_mismatch")
    subject, object_value = relational_fact_from_row(claim)
    rendered = render_relational_fact(
        subject=subject,
        predicate=str(claim["predicate"]),
        object_value=object_value,
    )
    if (
        claim["retrieval_text"] != rendered
        or claim["retrieval_text_sha256"] != sha256_text(rendered)
    ):
        raise ContractViolation("claim_retrieval_surface_mismatch")
    if claim["state_sha256"] != recompute_claim_state_sha256(claim):
        raise ContractViolation("claim_state_sha256_recomputation_mismatch")
    return owner, claim_id, revision_id, state


def _command_base(
    command: Mapping[str, Any], owner: UUID
) -> tuple[VerifiedActor, UUID, LifecycleAction, str]:
    actor = command.get("actor")
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_lifecycle_actor")
    require_owner(actor, owner)
    try:
        action = LifecycleAction(command.get("action"))
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_lifecycle_action") from exc
    operation_id = _uuid(
        command.get("operation_id"), "invalid_lifecycle_operation_id"
    )
    expected_state = require_sha256(
        command.get("expected_state_sha256"), "invalid_expected_state_sha256"
    )
    return actor, operation_id, action, expected_state


def _projection_delete(
    claim: Mapping[str, Any], *, operation_id: UUID, sequence_number: int
) -> dict[str, object]:
    owner = _uuid(claim["owner_user_id"], "invalid_projection_owner")
    claim_id = _uuid(claim["claim_id"], "invalid_projection_claim")
    revision_id = _uuid(claim["revision_id"], "invalid_projection_revision")
    manifest = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=operation_id,
        operation="delete",
        sequence_number=sequence_number,
        revision_sha256=str(claim["revision_sha256"]),
        selection_binding_sha256=str(claim["selection_binding_sha256"]),
        retrieval_text_sha256=str(claim["retrieval_text_sha256"]),
        embedding_input_sha256=str(claim["retrieval_text_sha256"]),
    )
    return {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "revision_id": str(revision_id),
        "operation_id": str(operation_id),
        "operation": "delete",
        "sequence_number": sequence_number,
        "revision_sha256": claim["revision_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "embedding_input_sha256": claim["retrieval_text_sha256"],
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": manifest,
        "state": "pending",
    }


def _revision_event(
    claim: Mapping[str, Any],
    *,
    operation_id: UUID,
    transition: str,
    occurred_at: datetime,
    prior_state_sha256: str,
    new_state_sha256: str,
) -> dict[str, object]:
    material = {
        "event_id": str(_derived_uuid(operation_id, f"revision-event-{transition}")),
        "owner_user_id": claim["owner_user_id"],
        "claim_id": claim["claim_id"],
        "revision_id": claim["revision_id"],
        "revision_sha256": claim["revision_sha256"],
        "operation_id": str(operation_id),
        "transition": transition,
        "occurred_at": occurred_at,
        "prior_state_sha256": prior_state_sha256,
        "new_state_sha256": new_state_sha256,
    }
    return {
        **material,
        "event_sha256": canonical_sha256(
            "governed_memory.claim_revision_event", material
        ),
    }


def _delete_receipt(
    value: object,
    *,
    claim: Mapping[str, Any],
    current_deletion_outbox: Mapping[str, Any],
    occurred_at: datetime,
) -> dict[str, object]:
    if (
        not isinstance(value, Mapping)
        or tuple(sorted(value)) != PROJECTION_DELETE_RECEIPT_FIELDS
    ):
        raise ContractViolation("invalid_projection_delete_receipt")
    if (
        not isinstance(current_deletion_outbox, Mapping)
        or set(current_deletion_outbox) != set(PROJECTION_OUTBOX_FIELDS)
        or current_deletion_outbox["state"] != "applied"
        or current_deletion_outbox["operation"] != "delete"
    ):
        raise ContractViolation("invalid_current_deletion_outbox")
    owner = _uuid(claim["owner_user_id"], "invalid_delete_receipt_owner")
    claim_id = _uuid(claim["claim_id"], "invalid_delete_receipt_claim")
    revision_id = _uuid(claim["revision_id"], "invalid_delete_receipt_revision")
    projection_operation_id = _uuid(
        current_deletion_outbox["operation_id"],
        "invalid_delete_outbox_operation",
    )
    sequence = require_exact_int(
        current_deletion_outbox["sequence_number"],
        code="invalid_delete_outbox_sequence",
        minimum=1,
    )
    if sequence != require_exact_int(
        claim["projection_sequence"],
        code="invalid_claim_projection_sequence",
        minimum=1,
    ):
        raise ContractViolation("delete_outbox_claim_sequence_mismatch")
    revision_sha256 = require_sha256(
        claim["revision_sha256"], "invalid_delete_claim_revision_sha256"
    )
    selection_sha256 = require_sha256(
        claim["selection_binding_sha256"],
        "invalid_delete_claim_selection_sha256",
    )
    retrieval_sha256 = require_sha256(
        claim["retrieval_text_sha256"],
        "invalid_delete_claim_retrieval_sha256",
    )
    outbox_expected = {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "revision_id": str(revision_id),
        "revision_sha256": revision_sha256,
        "selection_binding_sha256": selection_sha256,
        "retrieval_text_sha256": retrieval_sha256,
        "embedding_input_sha256": retrieval_sha256,
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
    }
    for field, expected in outbox_expected.items():
        if current_deletion_outbox[field] != expected:
            raise ContractViolation("delete_outbox_claim_binding_mismatch")
    expected_manifest = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=projection_operation_id,
        operation="delete",
        sequence_number=sequence,
        revision_sha256=revision_sha256,
        selection_binding_sha256=selection_sha256,
        retrieval_text_sha256=retrieval_sha256,
        embedding_input_sha256=retrieval_sha256,
    )
    if current_deletion_outbox["projection_manifest_sha256"] != expected_manifest:
        raise ContractViolation("delete_outbox_manifest_mismatch")
    if (
        _uuid(value["owner_user_id"], "invalid_delete_receipt_owner") != owner
        or _uuid(value["claim_id"], "invalid_delete_receipt_claim") != claim_id
        or _uuid(value["point_id"], "invalid_delete_receipt_point") != claim_id
        or _uuid(
            value["revision_id"], "invalid_delete_receipt_revision"
        )
        != revision_id
        or _uuid(
            value["projection_operation_id"],
            "invalid_delete_receipt_projection_operation",
        )
        != projection_operation_id
    ):
        raise ContractViolation("projection_delete_receipt_identity_mismatch")
    if value["operation"] != "delete" or value["state"] != "applied":
        raise ContractViolation("projection_delete_not_applied")
    receipt_sequence = require_exact_int(
        value["projection_sequence"],
        code="invalid_delete_receipt_sequence",
        minimum=1,
    )
    if receipt_sequence != sequence:
        raise ContractViolation("projection_delete_receipt_sequence_mismatch")
    applied = value["applied_at"]
    absence = value["absence_verified_at"]
    if not isinstance(applied, datetime) or not isinstance(absence, datetime):
        raise ContractViolation("invalid_delete_receipt_timestamp")
    applied = require_utc(applied, "invalid_delete_applied_timestamp")
    absence = require_utc(absence, "invalid_delete_absence_timestamp")
    if applied > absence or absence > occurred_at:
        raise ContractViolation("invalid_delete_receipt_timestamp_order")
    material = {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "collection_alias": require_key(
            value["collection_alias"], "invalid_delete_collection_alias"
        ),
        "embedding_input_sha256": retrieval_sha256,
        "operation": "delete",
        "physical_collection": require_key(
            value["physical_collection"],
            "invalid_delete_physical_collection",
        ),
        "point_id": str(claim_id),
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": expected_manifest,
        "projection_operation_id": str(projection_operation_id),
        "projection_sequence": sequence,
        "retrieval_text_sha256": retrieval_sha256,
        "revision_id": str(revision_id),
        "revision_sha256": revision_sha256,
        "selection_binding_sha256": selection_sha256,
        "absence_verification_sha256": require_sha256(
            value["absence_verification_sha256"],
            "invalid_delete_absence_verification_sha256",
        ),
        "absence_verified_at": absence,
        "applied_at": applied,
        "state": "applied",
    }
    for field in (
        "embedding_input_sha256",
        "projection_contract_sha256",
        "projection_manifest_sha256",
        "retrieval_text_sha256",
        "revision_sha256",
        "selection_binding_sha256",
    ):
        if value[field] != material[field]:
            raise ContractViolation("projection_delete_receipt_binding_mismatch")
    if value["receipt_sha256"] != canonical_sha256(
        "governed_memory.projection_delete_receipt", material
    ):
        raise ContractViolation("projection_delete_receipt_sha256_mismatch")
    return {**material, "receipt_sha256": value["receipt_sha256"]}


def _correction_proposal(
    claim: Mapping[str, Any],
    replacement: object,
    *,
    operation_id: UUID,
    expires_at: datetime,
    predicate_catalog: Mapping[str, Any],
) -> tuple[dict[str, object], dict[str, object], str]:
    if not isinstance(replacement, Mapping) or set(replacement) != {
        "object",
        "epistemic_state",
        "sensitivity",
    }:
        raise ContractViolation("invalid_correction_replacement")
    catalog = parse_predicate_catalog(predicate_catalog)
    subject_type = EntityKind(claim["subject_entity_type"])
    subject_input: dict[str, object] = {"kind": subject_type.value}
    if subject_type is not EntityKind.SELF:
        subject_input["label"] = claim["subject_display_name"]
    normalized = normalize_catalog_fact(
        {
            "subject": subject_input,
            "predicate": claim["predicate"],
            "object": replacement["object"],
            "epistemic_status": replacement["epistemic_state"],
            "sensitivity": replacement["sensitivity"],
        },
        catalog,
    )
    owner = _uuid(claim["owner_user_id"], "invalid_correction_owner")
    claim_id = _uuid(claim["claim_id"], "invalid_correction_claim")
    revision_id = _uuid(claim["revision_id"], "invalid_correction_revision")
    evidence_id = _derived_uuid(operation_id, "correction-evidence")
    source_message_id = uuid5(evidence_id, "source-message")
    source_thread_id = uuid5(evidence_id, "source-thread")
    source_window_id = uuid5(evidence_id, "source-window")
    source_material = {
        "subject_entity_key": claim["subject_entity_key"],
        "predicate": claim["predicate"],
        "object_kind": normalized["object_kind"],
        "object_entity_type": normalized["object_entity_type"],
        "object_display_name": normalized["object_display_name"],
        "object_literal": normalized["object_literal"],
        "epistemic_state": normalized["epistemic_state"],
        "sensitivity": normalized["sensitivity"],
    }
    source_bytes = canonical_json_bytes(source_material)
    source_sha256 = sha256_bytes(source_bytes)
    window_sha256 = framed_sha256(
        "governed_memory.correction_window.v1",
        (
            ("owner_user_id", str(owner)),
            ("claim_id", str(claim_id)),
            ("revision_id", str(revision_id)),
            ("operation_id", str(operation_id)),
            ("evidence_id", str(evidence_id)),
            ("source_message_id", str(source_message_id)),
            ("source_thread_id", str(source_thread_id)),
            ("source_window_id", str(source_window_id)),
            ("source_sha256", source_sha256),
        ),
    )
    selection = selection_binding_sha256(
        owner_user_id=owner,
        source_kind="owner_correction",
        source_message_id=source_message_id,
        source_thread_id=source_thread_id,
        source_window_id=source_window_id,
        window_sha256=window_sha256,
        source_sha256=source_sha256,
        selected_sha256=source_sha256,
        start_utf8=0,
        end_utf8=len(source_bytes),
        context_message_id=None,
        context_sha256=None,
    )
    object_kind = ObjectKind(normalized["object_kind"])
    object_entity_key = (
        derive_source_local_entity_key(
            owner_user_id=owner,
            selection_binding=selection,
            fact_index=0,
            role="object",
            entity_type=EntityKind(normalized["object_entity_type"]),
        )
        if object_kind is ObjectKind.ENTITY
        else None
    )
    semantic = semantic_key_sha256(
        subject_entity_key=str(claim["subject_entity_key"]),
        predicate=str(claim["predicate"]),
        object_kind=object_kind,
        object_entity_key=object_entity_key,
        object_literal=normalized["object_literal"],
    )
    proposal_id = uuid5(evidence_id, "correction-proposal")
    admission_operation_id = uuid5(evidence_id, "correction-admission")
    pending_claim = dict(claim)
    pending_claim["lifecycle_state"] = ClaimLifecycleState.CORRECTION_PENDING.value
    pending_claim["projection_sequence"] = int(claim["projection_sequence"]) + 1
    pending_claim["updated_at"] = expires_at
    pending_state_sha256 = recompute_claim_state_sha256(pending_claim)
    binding: dict[str, object] = {
        "owner_user_id": str(owner),
        "job_id": None,
        "evidence_id": str(evidence_id),
        "provider_call_id": None,
        "source_kind": "owner_correction",
        "source_message_id": str(source_message_id),
        "source_thread_id": str(source_thread_id),
        "source_window_id": str(source_window_id),
        "window_sha256": window_sha256,
        "source_sha256": source_sha256,
        "selected_sha256": source_sha256,
        "selection_binding_sha256": selection,
        "context_message_id": None,
        "context_sha256": None,
        "predicate_catalog_sha256": catalog.catalog_sha256,
        "request_sha256": None,
        "response_sha256": None,
        "purpose": ProposalPurpose.CORRECTION.value,
        "correction_target_claim_id": str(claim_id),
        "correction_target_revision_id": str(revision_id),
        "correction_target_revision_number": claim["revision_number"],
        "correction_target_revision_sha256": claim["revision_sha256"],
        "correction_target_state_sha256": claim["state_sha256"],
        "correction_target_identity_sha256": claim["claim_identity_sha256"],
        "correction_target_projection_sequence": claim["projection_sequence"],
        "correction_pending_state_sha256": pending_state_sha256,
    }
    item: dict[str, object] = {
        "epistemic_state": normalized["epistemic_state"],
        "fact_index": None,
        "object_display_name": normalized["object_display_name"],
        "object_entity_key": object_entity_key,
        "object_entity_type": normalized["object_entity_type"],
        "object_kind": normalized["object_kind"],
        "object_literal": normalized["object_literal"],
        "operation_id": str(admission_operation_id),
        "predicate": claim["predicate"],
        "proposal_id": str(proposal_id),
        "proposal_sha256": "0" * 64,
        "semantic_key_sha256": semantic,
        "sensitivity": normalized["sensitivity"],
        "subject_display_name": claim["subject_display_name"],
        "subject_entity_key": claim["subject_entity_key"],
        "subject_entity_type": claim["subject_entity_type"],
    }
    item["proposal_sha256"] = recompute_proposal_sha256(item, binding)
    record = {
        **binding,
        **item,
        "review_state": ProposalState.PENDING_REVIEW.value,
        "expires_at": expires_at,
    }
    return record, pending_claim, pending_state_sha256


def _receipt(
    claim: Mapping[str, Any],
    *,
    actor: VerifiedActor,
    operation_id: UUID,
    action: LifecycleAction,
    occurred_at: datetime,
    prior_state_sha256: str,
    resulting_state: str,
    command_material: Mapping[str, Any],
) -> dict[str, object]:
    reason_code = {
        LifecycleAction.CORRECT: "explicit_owner_correction",
        LifecycleAction.RETRACT: "explicit_owner_retraction",
        LifecycleAction.REQUEST_DELETE: "explicit_owner_deletion",
        LifecycleAction.FINALIZE_DELETE: "qdrant_delete_verified",
    }[action]
    material = {
        "owner_user_id_sha256": canonical_sha256(
            "governed_memory.owner", claim["owner_user_id"]
        ),
        "claim_id": claim["claim_id"],
        "operation_id": str(operation_id),
        "action": action.value,
        "prior_state_sha256": prior_state_sha256,
        "resulting_state": resulting_state,
        "reason_code": reason_code,
        "occurred_at": occurred_at,
        "actor_binding_sha256": actor.binding_sha256,
        "command_sha256": canonical_sha256(
            "governed_memory.lifecycle_command", command_material
        ),
        "external_model_calls": 0,
        "direct_vector_writes": 0,
    }
    return {
        **material,
        "receipt_sha256": canonical_sha256(
            "governed_memory.lifecycle_receipt", material
        ),
    }


def apply_lifecycle_command(
    claim: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    predicate_catalog: Mapping[str, Any],
    transaction_time: datetime,
    current_deletion_outbox: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    if not isinstance(command, Mapping):
        raise ContractViolation("invalid_lifecycle_input")
    owner, claim_id, revision_id, current_state = _validate_claim(
        claim, predicate_catalog
    )
    actor, operation_id, action, expected_state = _command_base(command, owner)
    occurred_at = require_utc(
        transaction_time, "invalid_lifecycle_transaction_time"
    )
    prior_state_sha256 = require_sha256(
        claim["state_sha256"], "invalid_claim_state_sha256"
    )
    if expected_state != prior_state_sha256:
        raise ContractViolation("claim_compare_and_swap_failed")
    base_fields = {
        "actor",
        "operation_id",
        "action",
        "expected_state_sha256",
    }

    if action is LifecycleAction.FINALIZE_DELETE:
        if set(command) != base_fields | {"projection_delete_receipt"}:
            raise ContractViolation("invalid_finalize_delete_command")
        if actor.role is not ActorRole.WORKER:
            raise ContractViolation("deletion_finalizer_role_denied")
        require_scope(actor, ActorScope.FINALIZE_DELETION)
        if current_state is not ClaimLifecycleState.DELETION_PENDING:
            raise ContractViolation("claim_not_pending_deletion")
        if current_deletion_outbox is None:
            raise ContractViolation("missing_current_deletion_outbox")
        delete_receipt = _delete_receipt(
            command["projection_delete_receipt"],
            claim=claim,
            current_deletion_outbox=current_deletion_outbox,
            occurred_at=occurred_at,
        )
        receipt = _receipt(
            claim,
            actor=actor,
            operation_id=operation_id,
            action=action,
            occurred_at=occurred_at,
            prior_state_sha256=prior_state_sha256,
            resulting_state=ClaimLifecycleState.DELETED.value,
            command_material={
                "actor_binding_sha256": actor.binding_sha256,
                "operation_id": operation_id,
                "action": action,
                "expected_state_sha256": expected_state,
                "projection_delete_receipt_sha256": delete_receipt["receipt_sha256"],
            },
        )
        return {
            "claim_lifecycle_state": None,
            "claim_state_sha256": None,
            "retrievable": False,
            "history_purged": True,
            "operation_outcome": OperationOutcome.APPLIED.value,
            "effects": [
                "verify_projection_delete_receipt",
                "purge_claim_content",
                "append_terminal_deletion_receipt",
            ],
            "projection_delete": None,
            "retained_projection_delete_receipt": delete_receipt,
            "replacement_proposal": None,
            "pending_correction_terminalization": None,
            "revision_event": None,
            "receipt": receipt,
        }

    if actor.role is not ActorRole.OWNER:
        raise ContractViolation("lifecycle_actor_role_denied")
    require_scope(actor, ActorScope.MUTATE_CLAIMS)
    if current_deletion_outbox is not None:
        raise ContractViolation("unexpected_current_deletion_outbox")
    override = None
    expected_fields = set(base_fields)
    if action in {LifecycleAction.RETRACT, LifecycleAction.REQUEST_DELETE}:
        expected_fields.add("expected_revision_sha256")
        if require_sha256(
            command.get("expected_revision_sha256"),
            "invalid_expected_revision_sha256",
        ) != claim["revision_sha256"]:
            raise ContractViolation("claim_revision_compare_and_swap_failed")
    if current_state is ClaimLifecycleState.CORRECTION_PENDING and action in {
        LifecycleAction.RETRACT,
        LifecycleAction.REQUEST_DELETE,
    }:
        override = {"terminal_reason": "lifecycle_override"}
    if action is LifecycleAction.CORRECT:
        expected_fields |= {
            "replacement",
            "expected_revision_sha256",
            "expected_predicate_catalog_sha256",
        }
        if set(command) != expected_fields:
            raise ContractViolation("invalid_correction_command")
        if current_state is ClaimLifecycleState.CORRECTION_PENDING:
            raise ContractViolation("correction_already_pending")
        if current_state is not ClaimLifecycleState.ACTIVE:
            raise ContractViolation("claim_not_correctable")
        if command["expected_revision_sha256"] != claim["revision_sha256"]:
            raise ContractViolation("claim_revision_compare_and_swap_failed")
        catalog = parse_predicate_catalog(predicate_catalog)
        if command["expected_predicate_catalog_sha256"] != catalog.catalog_sha256:
            raise ContractViolation("claim_catalog_compare_and_swap_failed")
        expires = occurred_at + CORRECTION_PROPOSAL_TTL
        replacement_proposal, new_claim, new_state_sha256 = _correction_proposal(
            claim,
            command["replacement"],
            operation_id=operation_id,
            expires_at=expires,
            predicate_catalog=predicate_catalog,
        )
        new_claim["updated_at"] = occurred_at
        new_state_sha256 = recompute_claim_state_sha256(new_claim)
        if (
            replacement_proposal["correction_pending_state_sha256"]
            != new_state_sha256
        ):
            raise ContractViolation("correction_pending_state_sha256_mismatch")
        new_state = ClaimLifecycleState.CORRECTION_PENDING
        transition = "correction_suppressed"
        effects = [
            "append_lifecycle_receipt",
            "append_correction_suppression_revision_event",
            "enqueue_projection_delete",
            "create_replacement_proposal",
        ]
    elif action is LifecycleAction.RETRACT:
        if set(command) != expected_fields:
            raise ContractViolation("invalid_retraction_command")
        if current_state not in {
            ClaimLifecycleState.ACTIVE,
            ClaimLifecycleState.CORRECTION_PENDING,
        }:
            raise ContractViolation("claim_not_retractable")
        new_state = ClaimLifecycleState.RETRACTED
        transition = "retracted"
        new_claim = dict(claim)
        new_claim["lifecycle_state"] = new_state.value
        new_claim["projection_sequence"] = int(claim["projection_sequence"]) + 1
        new_claim["updated_at"] = occurred_at
        new_state_sha256 = recompute_claim_state_sha256(new_claim)
        replacement_proposal = None
        effects = [
            "append_lifecycle_receipt",
            "append_retraction_revision_event",
            "enqueue_projection_delete",
        ]
    elif action is LifecycleAction.REQUEST_DELETE:
        if set(command) != expected_fields:
            raise ContractViolation("invalid_delete_request_command")
        if current_state in {
            ClaimLifecycleState.DELETION_PENDING,
            ClaimLifecycleState.DELETED,
        }:
            raise ContractViolation("claim_not_deletable")
        new_state = ClaimLifecycleState.DELETION_PENDING
        transition = "deletion_requested"
        new_claim = dict(claim)
        new_claim["lifecycle_state"] = new_state.value
        new_claim["projection_sequence"] = int(claim["projection_sequence"]) + 1
        new_claim["updated_at"] = occurred_at
        new_state_sha256 = recompute_claim_state_sha256(new_claim)
        replacement_proposal = None
        effects = ["append_lifecycle_receipt", "enqueue_projection_delete"]
    else:
        raise ContractViolation("invalid_lifecycle_action")
    if override is not None:
        effects.insert(0, "terminalize_pending_correction_lifecycle_override")
    projection_id = _derived_uuid(operation_id, "projection-delete")
    projection_delete = _projection_delete(
        claim,
        operation_id=projection_id,
        sequence_number=int(new_claim["projection_sequence"]),
    )
    revision_event = (
        _revision_event(
            claim,
            operation_id=operation_id,
            transition=transition,
            occurred_at=occurred_at,
            prior_state_sha256=prior_state_sha256,
            new_state_sha256=new_state_sha256,
        )
        if action in {LifecycleAction.CORRECT, LifecycleAction.RETRACT}
        else None
    )
    command_material = {
        "actor_binding_sha256": actor.binding_sha256,
        "operation_id": operation_id,
        "action": action,
        "expected_state_sha256": expected_state,
        "expected_revision_sha256": (
            command.get("expected_revision_sha256")
            if action
            in {
                LifecycleAction.CORRECT,
                LifecycleAction.RETRACT,
                LifecycleAction.REQUEST_DELETE,
            }
            else None
        ),
        "replacement_proposal_sha256": (
            replacement_proposal["proposal_sha256"]
            if replacement_proposal is not None
            else None
        ),
        "pending_correction_terminalization": override,
    }
    receipt = _receipt(
        claim,
        actor=actor,
        operation_id=operation_id,
        action=action,
        occurred_at=occurred_at,
        prior_state_sha256=prior_state_sha256,
        resulting_state=new_state.value,
        command_material=command_material,
    )
    return {
        "claim_lifecycle_state": new_state.value,
        "claim_state_sha256": new_state_sha256,
        "projection_sequence": new_claim["projection_sequence"],
        "retrievable": False,
        "history_purged": False,
        "operation_outcome": OperationOutcome.APPLIED.value,
        "effects": effects,
        "projection_delete": projection_delete,
        "retained_projection_delete_receipt": None,
        "replacement_proposal": replacement_proposal,
        "pending_correction_terminalization": override,
        "revision_event": revision_event,
        "receipt": receipt,
    }


__all__ = [
    "CORRECTION_PROPOSAL_TTL",
    "LifecycleAction",
    "apply_lifecycle_command",
]
