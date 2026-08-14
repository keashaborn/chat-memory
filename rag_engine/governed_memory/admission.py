from __future__ import annotations

"""Pure proposal review and authoritative claim construction."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .auth import ActorRole, ActorScope, VerifiedActor, require_owner, require_scope
from .contracts import (
    ClaimLifecycleState,
    ContractViolation,
    EntityKind,
    ObjectKind,
    OperationOutcome,
    ProjectionState,
    ProposalState,
    canonical_sha256,
    framed_sha256,
    render_relational_fact,
    require_exact_int,
    require_key,
    require_sha256,
    require_sorted_unique,
    require_utc,
    semantic_key_sha256,
    sha256_text,
    utc_text,
)
from .extraction import (
    COMPLETE_EXTRACTION_ITEM_FIELDS,
    PROPOSAL_HASH_BINDING_FIELDS,
    PredicateCatalog,
    ProposalPurpose,
    derive_fact_proposal_ids,
    parse_predicate_catalog,
    policy_for_sensitivity,
    recompute_proposal_sha256,
    validate_proposal_item,
)


_OWNER_ADMIT_REASON_CODES = ("explicit_owner_review",)
_AUTOMATIC_ADMIT_REASON_CODES = ("automatic_low_risk_owner_assertion",)
_REJECT_REASON_CODES = frozenset(
    {"duplicate_existing", "not_durable", "proposal_incorrect"}
)
AUTHORITATIVE_PROPOSAL_RECORD_FIELDS = tuple(
    sorted(
        set(COMPLETE_EXTRACTION_ITEM_FIELDS)
        | set(PROPOSAL_HASH_BINDING_FIELDS)
        | {"review_state", "expires_at"}
    )
)


class ReviewDecision(str, Enum):
    ADMIT = "admit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewCommand:
    actor: VerifiedActor
    proposal_id: UUID
    operation_id: UUID
    decision: ReviewDecision
    expected_proposal_sha256: str
    expected_source_sha256: str
    expected_selected_sha256: str
    expected_selection_binding_sha256: str
    expected_predicate_catalog_sha256: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.actor, VerifiedActor):
            raise ContractViolation("unverified_reviewer")
        if self.actor.role not in {ActorRole.OWNER, ActorRole.WORKER}:
            raise ContractViolation("reviewer_role_denied")
        if not isinstance(self.proposal_id, UUID) or not isinstance(
            self.operation_id, UUID
        ):
            raise ContractViolation("invalid_review_identifier")
        if not isinstance(self.decision, ReviewDecision):
            raise ContractViolation("invalid_review_decision")
        for value, code in (
            (self.expected_proposal_sha256, "invalid_expected_proposal_sha256"),
            (self.expected_source_sha256, "invalid_expected_source_sha256"),
            (self.expected_selected_sha256, "invalid_expected_selected_sha256"),
            (
                self.expected_selection_binding_sha256,
                "invalid_expected_selection_binding_sha256",
            ),
            (
                self.expected_predicate_catalog_sha256,
                "invalid_expected_predicate_catalog_sha256",
            ),
        ):
            require_sha256(value, code)
        valid_reasons = (
            (self.actor.role is ActorRole.OWNER
             and self.reason_codes == _OWNER_ADMIT_REASON_CODES)
            or (self.actor.role is ActorRole.WORKER
                and self.reason_codes == _AUTOMATIC_ADMIT_REASON_CODES)
        ) if self.decision is ReviewDecision.ADMIT else (
            self.actor.role is ActorRole.OWNER
            and bool(self.reason_codes)
            and tuple(sorted(set(self.reason_codes))) == self.reason_codes
            and all(reason in _REJECT_REASON_CODES for reason in self.reason_codes)
        )
        if not valid_reasons:
            raise ContractViolation("invalid_review_reason_codes")

    @property
    def command_sha256(self) -> str:
        return canonical_sha256(
            "governed_memory.review_command",
            {
                "actor_binding_sha256": self.actor.binding_sha256,
                "proposal_id": self.proposal_id,
                "operation_id": self.operation_id,
                "decision": self.decision,
                "expected_proposal_sha256": self.expected_proposal_sha256,
                "expected_source_sha256": self.expected_source_sha256,
                "expected_selected_sha256": self.expected_selected_sha256,
                "expected_selection_binding_sha256": (
                    self.expected_selection_binding_sha256
                ),
                "expected_predicate_catalog_sha256": (
                    self.expected_predicate_catalog_sha256
                ),
                "reason_codes": self.reason_codes,
            },
        )


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _derived_uuid(operation_id: UUID, purpose: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"governed-memory:{operation_id}:{purpose}")


def _parse_review_command(value: Mapping[str, Any]) -> ReviewCommand:
    required = {
        "actor",
        "proposal_id",
        "operation_id",
        "decision",
        "expected_proposal_sha256",
        "expected_source_sha256",
        "expected_selected_sha256",
        "expected_selection_binding_sha256",
        "expected_predicate_catalog_sha256",
        "reason_codes",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ContractViolation("invalid_review_command")
    actor = value["actor"]
    if not isinstance(actor, VerifiedActor):
        raise ContractViolation("unverified_reviewer")
    try:
        decision = ReviewDecision(value["decision"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_review_decision") from exc
    reasons = value["reason_codes"]
    if not isinstance(reasons, (tuple, list)):
        raise ContractViolation("invalid_review_reason_codes")
    return ReviewCommand(
        actor=actor,
        proposal_id=_uuid(value["proposal_id"], "invalid_review_proposal"),
        operation_id=_uuid(value["operation_id"], "invalid_review_operation"),
        decision=decision,
        expected_proposal_sha256=str(value["expected_proposal_sha256"]),
        expected_source_sha256=str(value["expected_source_sha256"]),
        expected_selected_sha256=str(value["expected_selected_sha256"]),
        expected_selection_binding_sha256=str(
            value["expected_selection_binding_sha256"]
        ),
        expected_predicate_catalog_sha256=str(
            value["expected_predicate_catalog_sha256"]
        ),
        reason_codes=tuple(reasons),
    )


def claim_identity_sha256(*, subject_entity_key: str, predicate: str) -> str:
    return framed_sha256(
        "governed_memory.claim_identity.v1",
        (
            (
                "subject_entity_key",
                require_key(subject_entity_key, "invalid_claim_subject_entity_key"),
            ),
            ("predicate", require_key(predicate, "invalid_claim_predicate")),
        ),
    )


def relational_fact_from_row(row: Mapping[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    try:
        subject_type = EntityKind(row["subject_entity_type"])
        object_kind = ObjectKind(row["object_kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation("invalid_relational_row") from exc
    subject = {
        "display_name": row["subject_display_name"],
        "entity_key": row["subject_entity_key"],
        "entity_type": subject_type.value,
    }
    if object_kind is ObjectKind.ENTITY:
        object_value = {
            "display_name": row["object_display_name"],
            "entity_key": row["object_entity_key"],
            "entity_type": row["object_entity_type"],
            "kind": object_kind.value,
        }
    else:
        object_value = {
            "kind": object_kind.value,
            "literal": row["object_literal"],
        }
    return subject, object_value


REVISION_HASH_FIELDS = (
    "owner_user_id",
    "claim_id",
    "revision_id",
    "revision_number",
    "proposal_id",
    "proposal_sha256",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "subject_entity_type",
    "subject_entity_key",
    "subject_display_name",
    "predicate",
    "object_kind",
    "object_entity_type",
    "object_entity_key",
    "object_display_name",
    "object_literal",
    "epistemic_state",
    "sensitivity",
    "projectable",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "valid_from",
    "valid_to",
    "source_sha256",
    "selected_sha256",
    "selection_binding_sha256",
    "predicate_catalog_sha256",
    "retrieval_text_sha256",
)

CLAIM_STATE_HASH_FIELDS = (
    "owner_user_id",
    "claim_id",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "lifecycle_state",
    "is_current",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "projection_sequence",
)


def _scalar(value: object, code: str) -> str | None:
    if value is None:
        return None
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        checked = require_sorted_unique(value, code=code)
        if checked:
            return "[" + ",".join(str(item) for item in checked) + "]"
        return "[]"
    raise ContractViolation(code)


def recompute_claim_state_sha256(claim: Mapping[str, Any]) -> str:
    if not isinstance(claim, Mapping) or any(
        field not in claim for field in CLAIM_STATE_HASH_FIELDS
    ):
        raise ContractViolation("claim_state_missing_field")
    return framed_sha256(
        "governed_memory.claim_state.v1",
        tuple(
            (field, _scalar(claim[field], f"invalid_claim_state_{field}"))
            for field in CLAIM_STATE_HASH_FIELDS
        ),
    )


def recompute_revision_sha256(revision: Mapping[str, Any]) -> str:
    if not isinstance(revision, Mapping) or any(
        field not in revision for field in REVISION_HASH_FIELDS
    ):
        raise ContractViolation("revision_hash_missing_field")
    return framed_sha256(
        "governed_memory.claim_revision.v1",
        tuple(
            (field, _scalar(revision[field], f"invalid_revision_{field}"))
            for field in REVISION_HASH_FIELDS
        ),
    )


CLAIM_HASH_TEST_VECTORS = MappingProxyType(
    {
        "revision": MappingProxyType(
            {
                "domain": "governed_memory.claim_revision.v1",
                "material": MappingProxyType(
                    {
                        "owner_user_id": "00000000-0000-0000-0000-000000000001",
                        "claim_id": "00000000-0000-0000-0000-000000000006",
                        "revision_id": "00000000-0000-0000-0000-000000000007",
                        "revision_number": 1,
                        "proposal_id": "00000000-0000-0000-0000-000000000009",
                        "proposal_sha256": (
                            "a8df61933716bb2809595922b6e24800ebfff064c9d667874ff511300ab4973c"
                        ),
                        "semantic_key_sha256": (
                            "3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc"
                        ),
                        "claim_identity_sha256": (
                            "dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1"
                        ),
                        "subject_entity_type": "self",
                        "subject_entity_key": "self",
                        "subject_display_name": None,
                        "predicate": "preference.personal",
                        "object_kind": "literal",
                        "object_entity_type": None,
                        "object_entity_key": None,
                        "object_display_name": None,
                        "object_literal": "café:\n猫",
                        "epistemic_state": "supported",
                        "sensitivity": "ordinary",
                        "projectable": True,
                        "domains": (),
                        "intents": (),
                        "surface": "normal",
                        "requires_explicit": False,
                        "valid_from": None,
                        "valid_to": None,
                        "source_sha256": "2" * 64,
                        "selected_sha256": "3" * 64,
                        "selection_binding_sha256": (
                            "20057004136f64e54333d53d4e46c85d85eba494c3140596f197572b0d47b619"
                        ),
                        "predicate_catalog_sha256": (
                            "5b1b31b9bc60e4727c9c70f8e634098536bd139a112f6193b29611fda60beded"
                        ),
                        "retrieval_text_sha256": (
                            "404e70a184945366051bd89614ea852e8530f46141f552ae2ff9a897e654004e"
                        ),
                    }
                ),
                "sha256": (
                    "61f011a52dceb6beec78f788b3638047bfa576d534ecc233bf8b059965ddef54"
                ),
            }
        ),
        "claim_state": MappingProxyType(
            {
                "domain": "governed_memory.claim_state.v1",
                "material": MappingProxyType(
                    {
                        "owner_user_id": "00000000-0000-0000-0000-000000000001",
                        "claim_id": "00000000-0000-0000-0000-000000000006",
                        "semantic_key_sha256": (
                            "3395c6bd0e42b2fc8340a5fcb0ce437b11d97d1811b32c544574ea8b0c360fdc"
                        ),
                        "claim_identity_sha256": (
                            "dff3fc2c55fd9bef445e1afa1f57a2bbac12fafd5ca8099b8f6a9b84da60b9a1"
                        ),
                        "lifecycle_state": "active",
                        "is_current": True,
                        "revision_id": "00000000-0000-0000-0000-000000000007",
                        "revision_number": 1,
                        "revision_sha256": (
                            "61f011a52dceb6beec78f788b3638047bfa576d534ecc233bf8b059965ddef54"
                        ),
                        "projection_sequence": 1,
                    }
                ),
                "sha256": (
                    "22444cef2c2c45950246c2d5e209386c0df41fe26793565e2967df85b898af68"
                ),
            }
        ),
    }
)


def _split_proposal_record(
    value: Mapping[str, Any],
) -> tuple[dict[str, object], dict[str, object], datetime]:
    if not isinstance(value, Mapping) or tuple(sorted(value)) != (
        AUTHORITATIVE_PROPOSAL_RECORD_FIELDS
    ):
        raise ContractViolation("invalid_authoritative_proposal_record")
    if value["review_state"] != ProposalState.PENDING_REVIEW.value:
        raise ContractViolation("proposal_not_pending_review")
    expires_at = value["expires_at"]
    if not isinstance(expires_at, datetime):
        raise ContractViolation("invalid_proposal_expiry")
    item = {field: value[field] for field in COMPLETE_EXTRACTION_ITEM_FIELDS}
    binding = {field: value[field] for field in PROPOSAL_HASH_BINDING_FIELDS}
    purpose = binding["purpose"]
    validate_proposal_item(
        item,
        allow_null_fact_index=purpose == ProposalPurpose.CORRECTION.value,
    )
    return item, binding, require_utc(expires_at, "invalid_proposal_expiry")


def _validate_catalog_fact(item: Mapping[str, Any], catalog: PredicateCatalog) -> None:
    rule = catalog.rule_for(str(item["predicate"]))
    subject_type = EntityKind(item["subject_entity_type"])
    object_kind = ObjectKind(item["object_kind"])
    from .contracts import EpistemicStatus, Sensitivity

    epistemic = EpistemicStatus(item["epistemic_state"])
    sensitivity = Sensitivity(item["sensitivity"])
    if subject_type not in rule.subject_kinds:
        raise ContractViolation("predicate_subject_kind_denied")
    if object_kind not in rule.object_kinds:
        raise ContractViolation("predicate_object_kind_denied")
    if epistemic not in rule.epistemic_statuses:
        raise ContractViolation("predicate_epistemic_status_denied")
    if sensitivity not in rule.sensitivities:
        raise ContractViolation("predicate_sensitivity_denied")


def _revision_sha256(
    *,
    owner_user_id: UUID,
    claim_id: UUID,
    revision_id: UUID,
    revision_number: int,
    proposal_id: UUID,
    proposal_sha256: str,
    item: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> str:
    policy = policy_for_sensitivity(item["sensitivity"])
    identity = claim_identity_sha256(
        subject_entity_key=str(item["subject_entity_key"]),
        predicate=str(item["predicate"]),
    )
    return recompute_revision_sha256(
        {
            "owner_user_id": str(owner_user_id),
            "claim_id": str(claim_id),
            "revision_id": str(revision_id),
            "revision_number": revision_number,
            "proposal_id": str(proposal_id),
            "proposal_sha256": proposal_sha256,
            "semantic_key_sha256": item["semantic_key_sha256"],
            "claim_identity_sha256": identity,
            "subject_entity_type": item["subject_entity_type"],
            "subject_entity_key": item["subject_entity_key"],
            "subject_display_name": item["subject_display_name"],
            "predicate": item["predicate"],
            "object_kind": item["object_kind"],
            "object_entity_type": item["object_entity_type"],
            "object_entity_key": item["object_entity_key"],
            "object_display_name": item["object_display_name"],
            "object_literal": item["object_literal"],
            "epistemic_state": item["epistemic_state"],
            "sensitivity": item["sensitivity"],
            "projectable": True,
            "domains": [],
            "intents": [],
            "surface": policy["surface"],
            "requires_explicit": policy["requires_explicit"],
            "valid_from": None,
            "valid_to": None,
            "source_sha256": binding["source_sha256"],
            "selected_sha256": binding["selected_sha256"],
            "selection_binding_sha256": binding["selection_binding_sha256"],
            "predicate_catalog_sha256": binding["predicate_catalog_sha256"],
            "retrieval_text_sha256": sha256_text(
                render_relational_fact(
                    subject=relational_fact_from_row(item)[0],
                    predicate=str(item["predicate"]),
                    object_value=relational_fact_from_row(item)[1],
                )
            ),
        }
    )


def _correction_restoration(
    binding: Mapping[str, Any], operation_id: UUID
) -> dict[str, object]:
    target_sequence = require_exact_int(
        int(str(binding["correction_target_projection_sequence"])),
        code="invalid_correction_target_projection_sequence",
    )
    material = {
        "claim_id": binding["correction_target_claim_id"],
        "expected_revision_id": binding["correction_target_revision_id"],
        "expected_revision_sha256": binding[
            "correction_target_revision_sha256"
        ],
        "expected_pre_transition_state_sha256": binding[
            "correction_target_state_sha256"
        ],
        "expected_pending_state_sha256": binding[
            "correction_pending_state_sha256"
        ],
        "expected_pending_projection_sequence": target_sequence + 1,
        "from_state": ClaimLifecycleState.CORRECTION_PENDING.value,
        "to_state": ClaimLifecycleState.ACTIVE.value,
        "restoration_projection_sequence": target_sequence + 2,
        "operation_id": str(operation_id),
    }
    return {
        **material,
        "recovery_sha256": canonical_sha256(
            "governed_memory.correction_recovery", material
        ),
    }


def apply_review(
    proposal_record: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    predicate_catalog: Mapping[str, Any],
    transaction_time: datetime,
) -> dict[str, object]:
    catalog = parse_predicate_catalog(predicate_catalog)
    item, binding, expires_at = _split_proposal_record(proposal_record)
    review = _parse_review_command(command)
    reviewed_at = require_utc(transaction_time, "invalid_review_transaction_time")
    owner = _uuid(binding["owner_user_id"], "invalid_proposal_owner")
    require_owner(review.actor, owner)
    require_scope(
        review.actor,
        ActorScope.REVIEW_PROPOSALS
        if review.actor.role is ActorRole.OWNER
        else ActorScope.AUTO_ADMIT_PROPOSALS,
    )
    if reviewed_at >= expires_at:
        raise ContractViolation("proposal_expired")
    proposal_id = _uuid(item["proposal_id"], "invalid_proposal_id")
    operation_id = _uuid(item["operation_id"], "invalid_proposal_operation")
    if review.proposal_id != proposal_id or review.operation_id != operation_id:
        raise ContractViolation("review_proposal_identity_mismatch")
    proposal_sha256 = require_sha256(
        item["proposal_sha256"], "invalid_proposal_sha256"
    )
    if proposal_sha256 != recompute_proposal_sha256(item, binding):
        raise ContractViolation("proposal_sha256_recomputation_mismatch")
    catalog_sha256 = require_sha256(
        binding["predicate_catalog_sha256"],
        "invalid_proposal_predicate_catalog_sha256",
    )
    if catalog_sha256 != catalog.catalog_sha256:
        raise ContractViolation("proposal_catalog_binding_mismatch")
    _validate_catalog_fact(item, catalog)
    expected_semantic = semantic_key_sha256(
        subject_entity_key=str(item["subject_entity_key"]),
        predicate=str(item["predicate"]),
        object_kind=str(item["object_kind"]),
        object_entity_key=item["object_entity_key"],
        object_literal=item["object_literal"],
    )
    if item["semantic_key_sha256"] != expected_semantic:
        raise ContractViolation("proposal_semantic_key_sha256_mismatch")
    for observed, expected, code in (
        (proposal_sha256, review.expected_proposal_sha256, "review_proposal_sha256_mismatch"),
        (binding["source_sha256"], review.expected_source_sha256, "review_source_sha256_mismatch"),
        (binding["selected_sha256"], review.expected_selected_sha256, "review_selected_sha256_mismatch"),
        (
            binding["selection_binding_sha256"],
            review.expected_selection_binding_sha256,
            "review_selection_binding_sha256_mismatch",
        ),
        (catalog_sha256, review.expected_predicate_catalog_sha256, "review_predicate_catalog_sha256_mismatch"),
    ):
        if observed != expected:
            raise ContractViolation(code)

    purpose = ProposalPurpose(binding["purpose"])
    fact_index = item["fact_index"]
    if purpose is ProposalPurpose.NEW_CLAIM:
        provider_call_id = _uuid(
            binding["provider_call_id"], "invalid_proposal_provider_call"
        )
        expected_proposal_id, expected_operation_id = derive_fact_proposal_ids(
            provider_call_id=provider_call_id,
            fact_index=require_exact_int(
                fact_index, code="invalid_proposal_fact_index", maximum=7
            ),
            normalized_fact=item,
            selection_binding=str(binding["selection_binding_sha256"]),
        )
        if proposal_id != expected_proposal_id or operation_id != expected_operation_id:
            raise ContractViolation("proposal_deterministic_identity_mismatch")
    else:
        evidence_id = _uuid(binding["evidence_id"], "invalid_proposal_evidence")
        if proposal_id != uuid5(evidence_id, "correction-proposal") or operation_id != uuid5(
            evidence_id, "correction-admission"
        ):
            raise ContractViolation("correction_deterministic_identity_mismatch")
        expected_identity = require_sha256(
            binding["correction_target_identity_sha256"],
            "invalid_correction_target_identity_sha256",
        )
        if expected_identity != claim_identity_sha256(
            subject_entity_key=str(item["subject_entity_key"]),
            predicate=str(item["predicate"]),
        ):
            raise ContractViolation("correction_identity_change_prohibited")

    if review.actor.role is ActorRole.WORKER and (
        purpose is not ProposalPurpose.NEW_CLAIM
        or binding["source_kind"] != "conversation_message"
        or item["subject_entity_type"] != "self"
        or item["subject_entity_key"] != "self"
        or item["epistemic_state"] != "supported"
        or item["sensitivity"] != "ordinary"
    ):
        raise ContractViolation("automatic_admission_policy_denied")

    receipt_material: dict[str, object] = {
        "owner_user_id_sha256": canonical_sha256("governed_memory.owner", str(owner)),
        "proposal_id": str(proposal_id),
        "operation_id": str(operation_id),
        "decision": review.decision.value,
        "command_sha256": review.command_sha256,
        "proposal_sha256": proposal_sha256,
        "source_sha256": binding["source_sha256"],
        "selected_sha256": binding["selected_sha256"],
        "selection_binding_sha256": binding["selection_binding_sha256"],
        "predicate_catalog_sha256": catalog_sha256,
        "proposal_purpose": purpose.value,
        "reviewed_at": reviewed_at,
        "reason_codes": list(review.reason_codes),
        "external_model_calls": 0,
        "direct_vector_writes": 0,
    }
    receipt = dict(receipt_material)
    receipt["receipt_sha256"] = canonical_sha256(
        "governed_memory.review_receipt", receipt_material
    )
    if review.decision is ReviewDecision.REJECT:
        recovery = None
        effects = ["append_review_receipt"]
        if purpose is ProposalPurpose.CORRECTION:
            recovery = _correction_restoration(binding, operation_id)
            effects.append(
                "restore_suppressed_correction_target_and_enqueue_projection"
            )
        return {
            "proposal_state": ProposalState.REJECTED.value,
            "operation_outcome": OperationOutcome.APPLIED.value,
            "effects": effects,
            "claim": None,
            "revision": None,
            "projection": None,
            "correction_recovery": recovery,
            "receipt": receipt,
        }

    if purpose is ProposalPurpose.NEW_CLAIM:
        claim_id = _derived_uuid(operation_id, "claim")
        revision_number = 1
        projection_sequence = 1
    else:
        claim_id = _uuid(
            binding["correction_target_claim_id"], "invalid_correction_target_claim"
        )
        revision_number = require_exact_int(
            int(str(binding["correction_target_revision_number"])),
            code="invalid_correction_target_revision_number",
            minimum=1,
        ) + 1
        projection_sequence = require_exact_int(
            int(str(binding["correction_target_projection_sequence"])),
            code="invalid_correction_target_projection_sequence",
        ) + 2
    revision_id = _derived_uuid(operation_id, "revision")
    projection_id = _derived_uuid(operation_id, "projection")
    policy = policy_for_sensitivity(item["sensitivity"])
    subject, object_value = relational_fact_from_row(item)
    retrieval_text = render_relational_fact(
        subject=subject,
        predicate=str(item["predicate"]),
        object_value=object_value,
    )
    retrieval_text_sha256 = sha256_text(retrieval_text)
    identity_sha256 = claim_identity_sha256(
        subject_entity_key=str(item["subject_entity_key"]),
        predicate=str(item["predicate"]),
    )
    revision_sha256 = _revision_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        revision_number=revision_number,
        proposal_id=proposal_id,
        proposal_sha256=proposal_sha256,
        item=item,
        binding=binding,
    )
    claim: dict[str, object] = {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "revision_id": str(revision_id),
        "revision_number": revision_number,
        "revision_sha256": revision_sha256,
        "projection_sequence": projection_sequence,
        "lifecycle_state": ClaimLifecycleState.ACTIVE.value,
        "is_current": True,
        "projectable": True,
        "predicate_catalog_sha256": catalog_sha256,
        "predicate": item["predicate"],
        "subject_entity_type": item["subject_entity_type"],
        "subject_entity_key": item["subject_entity_key"],
        "subject_display_name": item["subject_display_name"],
        "object_kind": item["object_kind"],
        "object_entity_type": item["object_entity_type"],
        "object_entity_key": item["object_entity_key"],
        "object_display_name": item["object_display_name"],
        "object_literal": item["object_literal"],
        "epistemic_state": item["epistemic_state"],
        "sensitivity": item["sensitivity"],
        "domains": [],
        "intents": [],
        "surface": policy["surface"],
        "requires_explicit": policy["requires_explicit"],
        "selection_binding_sha256": binding["selection_binding_sha256"],
        "source_sha256": binding["source_sha256"],
        "valid_from": None,
        "valid_to": None,
        "updated_at": reviewed_at,
        "retrieval_text": retrieval_text,
        "retrieval_text_sha256": retrieval_text_sha256,
        "semantic_key_sha256": item["semantic_key_sha256"],
        "claim_identity_sha256": identity_sha256,
    }
    claim["state_sha256"] = recompute_claim_state_sha256(claim)
    revision = {
        "owner_user_id": claim["owner_user_id"],
        "claim_id": claim["claim_id"],
        "revision_id": claim["revision_id"],
        "revision_number": claim["revision_number"],
        "proposal_id": str(proposal_id),
        "proposal_sha256": proposal_sha256,
        "semantic_key_sha256": claim["semantic_key_sha256"],
        "claim_identity_sha256": claim["claim_identity_sha256"],
        "subject_entity_type": claim["subject_entity_type"],
        "subject_entity_key": claim["subject_entity_key"],
        "subject_display_name": claim["subject_display_name"],
        "predicate": claim["predicate"],
        "object_kind": claim["object_kind"],
        "object_entity_type": claim["object_entity_type"],
        "object_entity_key": claim["object_entity_key"],
        "object_display_name": claim["object_display_name"],
        "object_literal": claim["object_literal"],
        "epistemic_state": claim["epistemic_state"],
        "sensitivity": claim["sensitivity"],
        "projectable": claim["projectable"],
        "domains": claim["domains"],
        "intents": claim["intents"],
        "surface": claim["surface"],
        "requires_explicit": claim["requires_explicit"],
        "valid_from": claim["valid_from"],
        "valid_to": claim["valid_to"],
        "source_sha256": claim["source_sha256"],
        "selected_sha256": binding["selected_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "predicate_catalog_sha256": claim["predicate_catalog_sha256"],
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "revision_sha256": revision_sha256,
        "state_sha256": claim["state_sha256"],
        "evidence_id": binding["evidence_id"],
        "purpose": purpose.value,
    }
    if recompute_revision_sha256(revision) != revision_sha256:
        raise ContractViolation("revision_sha256_internal_mismatch")
    from .projection import (
        PROJECTION_CONTRACT_SHA256,
        projection_manifest_sha256,
    )

    projection_manifest = projection_manifest_sha256(
        owner_user_id=owner,
        claim_id=claim_id,
        revision_id=revision_id,
        operation_id=projection_id,
        operation="upsert",
        sequence_number=projection_sequence,
        revision_sha256=revision_sha256,
        selection_binding_sha256=str(binding["selection_binding_sha256"]),
        retrieval_text_sha256=retrieval_text_sha256,
        embedding_input_sha256=retrieval_text_sha256,
    )
    projection = {
        "owner_user_id": str(owner),
        "claim_id": str(claim_id),
        "revision_id": str(revision_id),
        "operation_id": str(projection_id),
        "operation": "upsert",
        "state": ProjectionState.PENDING.value,
        "sequence_number": projection_sequence,
        "revision_sha256": revision_sha256,
        "selection_binding_sha256": binding["selection_binding_sha256"],
        "retrieval_text_sha256": retrieval_text_sha256,
        "embedding_input_sha256": retrieval_text_sha256,
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": projection_manifest,
    }
    return {
        "proposal_state": ProposalState.ADMITTED.value,
        "operation_outcome": OperationOutcome.APPLIED.value,
        "effects": [
            "append_review_receipt",
            "activate_corrected_claim"
            if purpose is ProposalPurpose.CORRECTION
            else "create_claim",
            "create_claim_revision",
            "link_claim_evidence",
            "enqueue_projection",
        ],
        "claim": claim,
        "revision": revision,
        "projection": projection,
        "correction_recovery": None,
        "receipt": receipt,
    }


def expire_proposal(
    proposal_record: Mapping[str, Any],
    *,
    predicate_catalog: Mapping[str, Any],
    transaction_time: datetime,
) -> dict[str, object]:
    """Plan trusted-clock expiry, including correction restoration."""

    catalog = parse_predicate_catalog(predicate_catalog)
    item, binding, expires_at = _split_proposal_record(proposal_record)
    expired_at = require_utc(
        transaction_time, "invalid_expiration_transaction_time"
    )
    if expired_at < expires_at:
        raise ContractViolation("proposal_not_expired")
    proposal_sha256 = require_sha256(
        item["proposal_sha256"], "invalid_proposal_sha256"
    )
    if proposal_sha256 != recompute_proposal_sha256(item, binding):
        raise ContractViolation("proposal_sha256_recomputation_mismatch")
    if binding["predicate_catalog_sha256"] != catalog.catalog_sha256:
        raise ContractViolation("proposal_catalog_binding_mismatch")
    _validate_catalog_fact(item, catalog)
    purpose = ProposalPurpose(binding["purpose"])
    operation_id = _uuid(item["operation_id"], "invalid_proposal_operation")
    recovery = (
        _correction_restoration(binding, operation_id)
        if purpose is ProposalPurpose.CORRECTION
        else None
    )
    receipt_material = {
        "owner_user_id_sha256": canonical_sha256(
            "governed_memory.owner", str(binding["owner_user_id"])
        ),
        "proposal_id": str(_uuid(item["proposal_id"], "invalid_proposal_id")),
        "operation_id": str(operation_id),
        "decision": "expire",
        "proposal_sha256": proposal_sha256,
        "selection_binding_sha256": binding["selection_binding_sha256"],
        "predicate_catalog_sha256": catalog.catalog_sha256,
        "proposal_purpose": purpose.value,
        "expires_at": expires_at,
        "expired_at": expired_at,
        "external_model_calls": 0,
        "direct_vector_writes": 0,
    }
    receipt = {
        **receipt_material,
        "receipt_sha256": canonical_sha256(
            "governed_memory.proposal_expiration_receipt", receipt_material
        ),
    }
    effects = ["append_proposal_expiration_receipt"]
    if recovery is not None:
        effects.append(
            "restore_suppressed_correction_target_and_enqueue_projection"
        )
    return {
        "proposal_state": ProposalState.EXPIRED.value,
        "operation_outcome": OperationOutcome.APPLIED.value,
        "effects": effects,
        "claim": None,
        "revision": None,
        "projection": None,
        "correction_recovery": recovery,
        "receipt": receipt,
    }


__all__ = [
    "AUTHORITATIVE_PROPOSAL_RECORD_FIELDS",
    "CLAIM_HASH_TEST_VECTORS",
    "CLAIM_STATE_HASH_FIELDS",
    "REVISION_HASH_FIELDS",
    "ReviewCommand",
    "ReviewDecision",
    "apply_review",
    "claim_identity_sha256",
    "expire_proposal",
    "recompute_claim_state_sha256",
    "recompute_revision_sha256",
    "relational_fact_from_row",
]
