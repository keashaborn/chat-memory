"""Pure compare-and-swap claim lifecycle tests."""

from __future__ import annotations

import copy
from datetime import timedelta
import inspect
import json
import unittest
from uuid import UUID

from rag_engine.governed_memory.admission import apply_review, expire_proposal
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.contracts import ContractViolation, OperationOutcome
from rag_engine.governed_memory.lifecycle import (
    CORRECTION_PROPOSAL_TTL,
    apply_lifecycle_command,
)
from rag_engine.governed_memory.projection import (
    PROJECTION_CONTRACT_SHA256,
    build_projection_delete,
    build_projection_delete_receipt,
    projection_manifest_sha256,
)
from tests.memory._fixtures import (
    NOW,
    OPERATION_A,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    PROJECTION_A,
    SOURCE_TEXT,
    make_claim_row,
)


REPLACEMENT = {
    "object": {"kind": "literal", "value": "amber"},
    "epistemic_state": "supported",
    "sensitivity": "ordinary",
}


def actor(
    *,
    owner=OWNER_A,
    actor_id=None,
    role=ActorRole.OWNER,
    scopes=(ActorScope.MUTATE_CLAIMS,),
) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=owner,
        actor_id=owner if actor_id is None else actor_id,
        session_id=owner,
        role=role,
        scopes=scopes,
        authentication_manifest_sha256="a" * 64,
        authenticated_at=NOW,
    )


def delete_outbox(claim: dict[str, object], *, state: str = "applied") -> dict[str, object]:
    sequence = int(claim["projection_sequence"])
    manifest = projection_manifest_sha256(
        owner_user_id=UUID(str(claim["owner_user_id"])),
        claim_id=UUID(str(claim["claim_id"])),
        revision_id=UUID(str(claim["revision_id"])),
        operation_id=PROJECTION_A,
        operation="delete",
        sequence_number=sequence,
        revision_sha256=str(claim["revision_sha256"]),
        selection_binding_sha256=str(claim["selection_binding_sha256"]),
        retrieval_text_sha256=str(claim["retrieval_text_sha256"]),
        embedding_input_sha256=str(claim["retrieval_text_sha256"]),
    )
    return {
        "owner_user_id": claim["owner_user_id"],
        "claim_id": claim["claim_id"],
        "revision_id": claim["revision_id"],
        "operation_id": str(PROJECTION_A),
        "operation": "delete",
        "sequence_number": sequence,
        "revision_sha256": claim["revision_sha256"],
        "selection_binding_sha256": claim["selection_binding_sha256"],
        "retrieval_text_sha256": claim["retrieval_text_sha256"],
        "embedding_input_sha256": claim["retrieval_text_sha256"],
        "projection_contract_sha256": PROJECTION_CONTRACT_SHA256,
        "projection_manifest_sha256": manifest,
        "state": state,
    }


def delete_receipt(claim: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    claimed = delete_outbox(claim, state="claimed")
    delete_command = build_projection_delete(
        claimed,
        collection_alias="governed_memory_active",
        physical_collection="governed_memory_build_synthetic",
    )
    receipt = build_projection_delete_receipt(
        delete_command,
        applied_at=NOW - timedelta(seconds=2),
        absence_verified_at=NOW - timedelta(seconds=1),
        absence_verification_sha256="f" * 64,
    )
    return {**claimed, "state": "applied"}, receipt


def command(claim: dict[str, object], action: str) -> dict[str, object]:
    value: dict[str, object] = {
        "actor": actor(),
        "operation_id": str(OPERATION_A),
        "action": action,
        "expected_state_sha256": claim["state_sha256"],
    }
    if action == "correct":
        value.update(
            {
                "expected_revision_sha256": claim["revision_sha256"],
                "expected_predicate_catalog_sha256": claim[
                    "predicate_catalog_sha256"
                ],
                "replacement": copy.deepcopy(REPLACEMENT),
            }
        )
    if action in {"retract", "request_delete"}:
        value["expected_revision_sha256"] = claim["revision_sha256"]
    if action == "finalize_delete":
        _, receipt = delete_receipt(claim)
        value["actor"] = actor(
            actor_id=OWNER_B,
            role=ActorRole.WORKER,
            scopes=(ActorScope.FINALIZE_DELETION,),
        )
        value["projection_delete_receipt"] = receipt
    return value


def transition(
    claim: dict[str, object],
    value: dict[str, object],
    *,
    transaction_time=NOW,
    current_deletion_outbox=None,
):
    return apply_lifecycle_command(
        claim,
        value,
        predicate_catalog=PREDICATE_CATALOG,
        transaction_time=transaction_time,
        current_deletion_outbox=current_deletion_outbox,
    )


class LifecycleTests(unittest.TestCase):
    def test_pending_correction_override_is_server_derived_not_caller_input(self) -> None:
        self.assertNotIn(
            "pending_correction_override",
            inspect.signature(apply_lifecycle_command).parameters,
        )
        claim = make_claim_row()
        correction = transition(claim, command(claim, "correct"))
        pending = {
            **claim,
            "lifecycle_state": correction["claim_lifecycle_state"],
            "projection_sequence": correction["projection_sequence"],
            "state_sha256": correction["claim_state_sha256"],
            "updated_at": NOW,
        }
        retraction = command(pending, "retract")
        result = transition(pending, retraction)
        self.assertEqual(
            result["pending_correction_terminalization"],
            {"terminal_reason": "lifecycle_override"},
        )

        caller_override = copy.deepcopy(retraction)
        caller_override["pending_correction_override"] = {
            "proposal_id": "00000000-0000-4000-8000-000000000001",
            "proposal_sha256": "f" * 64,
        }
        with self.assertRaisesRegex(
            ContractViolation, "invalid_retraction_command"
        ):
            transition(pending, caller_override)

    def test_correction_immediately_suppresses_and_creates_reviewable_replacement(self) -> None:
        claim = make_claim_row()
        result = transition(claim, command(claim, "correct"))
        self.assertEqual(result["claim_lifecycle_state"], "correction_pending")
        self.assertFalse(result["retrievable"])
        self.assertEqual(
            result["effects"],
            [
                "append_lifecycle_receipt",
                "append_correction_suppression_revision_event",
                "enqueue_projection_delete",
                "create_replacement_proposal",
            ],
        )
        proposal = result["replacement_proposal"]
        self.assertEqual(proposal["review_state"], "pending_review")
        self.assertEqual(proposal["purpose"], "correction")
        self.assertEqual(proposal["correction_target_claim_id"], claim["claim_id"])
        self.assertEqual(
            proposal["correction_target_revision_id"], claim["revision_id"]
        )
        self.assertEqual(
            proposal["predicate_catalog_sha256"], claim["predicate_catalog_sha256"]
        )
        self.assertEqual(proposal["expires_at"], NOW + CORRECTION_PROPOSAL_TTL)
        self.assertEqual(result["operation_outcome"], OperationOutcome.APPLIED.value)
        self.assertEqual(result["projection_sequence"], 2)
        self.assertIsNotNone(result["revision_event"])
        self.assertEqual(result["receipt"]["reason_code"], "explicit_owner_correction")

    def test_correction_lineage_and_replay_are_deterministic_and_content_free(self) -> None:
        claim = make_claim_row()
        first = transition(claim, command(claim, "correct"))["replacement_proposal"]
        replay = transition(claim, command(claim, "correct"))["replacement_proposal"]
        self.assertEqual(first, replay)
        self.assertEqual(first["source_kind"], "owner_correction")
        self.assertEqual(first["source_sha256"], first["selected_sha256"])
        self.assertIsNone(first["context_message_id"])
        self.assertIsNone(first["context_sha256"])
        lineage = (
            UUID(first["evidence_id"]),
            UUID(first["source_message_id"]),
            UUID(first["source_thread_id"]),
            UUID(first["source_window_id"]),
        )
        self.assertEqual(len(set(lineage)), len(lineage))
        self.assertNotIn(SOURCE_TEXT, json.dumps(first, default=str))

        changed = command(claim, "correct")
        changed["operation_id"] = "13131313-1313-4313-8313-131313131313"
        other = transition(claim, changed)["replacement_proposal"]
        for field in (
            "proposal_id",
            "operation_id",
            "evidence_id",
            "source_message_id",
            "source_thread_id",
            "source_window_id",
            "window_sha256",
            "selection_binding_sha256",
        ):
            with self.subTest(field=field):
                self.assertNotEqual(other[field], first[field])

    def test_correction_proposal_is_directly_review_compatible(self) -> None:
        claim = make_claim_row()
        proposal = transition(claim, command(claim, "correct"))[
            "replacement_proposal"
        ]
        reviewed = apply_review(
            proposal,
            {
                "actor": actor(scopes=(ActorScope.REVIEW_PROPOSALS,)),
                "proposal_id": proposal["proposal_id"],
                "operation_id": proposal["operation_id"],
                "decision": "admit",
                "expected_proposal_sha256": proposal["proposal_sha256"],
                "expected_source_sha256": proposal["source_sha256"],
                "expected_selected_sha256": proposal["selected_sha256"],
                "expected_selection_binding_sha256": proposal[
                    "selection_binding_sha256"
                ],
                "expected_predicate_catalog_sha256": proposal[
                    "predicate_catalog_sha256"
                ],
                "reason_codes": ["explicit_owner_review"],
            },
            predicate_catalog=PREDICATE_CATALOG,
            transaction_time=NOW,
        )
        self.assertEqual(reviewed["claim"]["claim_id"], claim["claim_id"])
        self.assertEqual(reviewed["claim"]["revision_number"], 2)
        self.assertEqual(reviewed["claim"]["object_literal"], "amber")
        self.assertEqual(reviewed["claim"]["projection_sequence"], 3)
        self.assertIn("activate_corrected_claim", reviewed["effects"])

    def test_correction_expiry_restores_suppressed_claim_symmetrically(self) -> None:
        claim = make_claim_row()
        proposal = transition(claim, command(claim, "correct"))[
            "replacement_proposal"
        ]
        expires_at = proposal["expires_at"]
        with self.assertRaises(ContractViolation):
            expire_proposal(
                proposal,
                predicate_catalog=PREDICATE_CATALOG,
                transaction_time=expires_at - timedelta(microseconds=1),
            )
        expired = expire_proposal(
            proposal,
            predicate_catalog=PREDICATE_CATALOG,
            transaction_time=expires_at,
        )
        self.assertEqual(expired["proposal_state"], "expired")
        self.assertEqual(
            expired["effects"],
            [
                "append_proposal_expiration_receipt",
                "restore_suppressed_correction_target_and_enqueue_projection",
            ],
        )
        recovery = expired["correction_recovery"]
        self.assertEqual(recovery["claim_id"], claim["claim_id"])
        self.assertEqual(recovery["expected_revision_id"], claim["revision_id"])
        self.assertEqual(recovery["from_state"], "correction_pending")
        self.assertEqual(recovery["to_state"], "active")
        self.assertEqual(recovery["restoration_projection_sequence"], 3)
        self.assertNotIn(SOURCE_TEXT, json.dumps(expired["receipt"], default=str))

    def test_correction_rejects_free_form_extra_and_legacy_fields(self) -> None:
        claim = make_claim_row()
        free_form = command(claim, "correct")
        free_form["replacement"] = "Synthetic prose is not a fact contract."
        extra = command(claim, "correct")
        extra["replacement"]["untrusted_extra"] = True
        legacy = command(claim, "correct")
        legacy["replacement_text"] = "amber"
        for malformed in (free_form, extra, legacy):
            with self.subTest(malformed=malformed), self.assertRaises(
                ContractViolation
            ):
                transition(claim, malformed)

    def test_correction_revision_and_catalog_mismatches_fail_closed(self) -> None:
        claim = make_claim_row()
        for field in (
            "expected_revision_sha256",
            "expected_predicate_catalog_sha256",
        ):
            malformed = command(claim, "correct")
            malformed[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                transition(claim, malformed)

    def test_retraction_and_delete_request_are_immediately_nonretrievable(self) -> None:
        claim = make_claim_row()
        retracted = transition(claim, command(claim, "retract"))
        self.assertEqual(retracted["claim_lifecycle_state"], "retracted")
        self.assertFalse(retracted["retrievable"])
        self.assertIn("enqueue_projection_delete", retracted["effects"])
        self.assertIsNotNone(retracted["revision_event"])
        self.assertFalse(retracted["history_purged"])
        self.assertEqual(
            retracted["receipt"]["reason_code"], "explicit_owner_retraction"
        )

        deletion = transition(claim, command(claim, "request_delete"))
        self.assertEqual(deletion["claim_lifecycle_state"], "deletion_pending")
        self.assertFalse(deletion["retrievable"])
        self.assertIn("enqueue_projection_delete", deletion["effects"])
        self.assertIsNone(deletion["revision_event"])
        self.assertFalse(deletion["history_purged"])
        self.assertEqual(
            deletion["receipt"]["reason_code"], "explicit_owner_deletion"
        )

        for action in ("correct", "retract", "request_delete"):
            forged = command(claim, action)
            forged["reason_code"] = "caller_controls_reason"
            with self.subTest(action=action), self.assertRaises(ContractViolation):
                transition(claim, forged)

    def test_retraction_and_delete_require_exact_revision_compare_and_swap(self) -> None:
        claim = make_claim_row()
        for action in ("retract", "request_delete"):
            missing = command(claim, action)
            del missing["expected_revision_sha256"]
            stale = command(claim, action)
            stale["expected_revision_sha256"] = "0" * 64
            for malformed in (missing, stale):
                with self.subTest(action=action, command=malformed), self.assertRaises(
                    ContractViolation
                ):
                    transition(claim, malformed)

    def test_final_delete_requires_exact_current_applied_outbox_and_receipt(self) -> None:
        claim = make_claim_row(lifecycle_state="deletion_pending")
        outbox, receipt = delete_receipt(claim)
        value = command(claim, "finalize_delete")
        value["projection_delete_receipt"] = receipt

        for malformed_outbox in (None, {**outbox, "state": "claimed"}):
            with self.subTest(outbox=malformed_outbox), self.assertRaises(
                ContractViolation
            ):
                transition(
                    claim,
                    value,
                    current_deletion_outbox=malformed_outbox,
                )

        for field, replacement in (
            ("absence_verification_sha256", "0" * 64),
            ("projection_sequence", int(claim["projection_sequence"]) + 1),
            ("point_id", str(OWNER_B)),
        ):
            malformed = copy.deepcopy(value)
            malformed["projection_delete_receipt"][field] = replacement
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                transition(claim, malformed, current_deletion_outbox=outbox)

        result = transition(claim, value, current_deletion_outbox=outbox)
        self.assertIsNone(result["claim_lifecycle_state"])
        self.assertTrue(result["history_purged"])
        self.assertEqual(
            result["effects"],
            [
                "verify_projection_delete_receipt",
                "purge_claim_content",
                "append_terminal_deletion_receipt",
            ],
        )
        self.assertEqual(result["retained_projection_delete_receipt"], receipt)

    def test_owner_cannot_finalize_delete(self) -> None:
        claim = make_claim_row(lifecycle_state="deletion_pending")
        outbox, _ = delete_receipt(claim)
        unauthorized = command(claim, "finalize_delete")
        unauthorized["actor"] = actor()
        with self.assertRaises(ContractViolation):
            transition(claim, unauthorized, current_deletion_outbox=outbox)

    def test_terminal_deletion_receipts_are_content_free(self) -> None:
        claim = make_claim_row(lifecycle_state="deletion_pending")
        outbox, _ = delete_receipt(claim)
        result = transition(
            claim,
            command(claim, "finalize_delete"),
            current_deletion_outbox=outbox,
        )
        serialized = json.dumps(result, sort_keys=True, default=str)
        self.assertNotIn(SOURCE_TEXT, serialized)
        self.assertNotIn(str(claim["object_literal"]), serialized)
        self.assertNotIn("replacement_text", serialized)
        self.assertRegex(result["receipt"]["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_compare_and_swap_state_fact_policy_owner_and_scope_fail_closed(self) -> None:
        baseline = make_claim_row()
        cases: list[tuple[str, dict[str, object], dict[str, object]]] = []
        stale = command(baseline, "retract")
        stale["expected_state_sha256"] = "0" * 64
        cases.append(("state", baseline, stale))
        for field, replacement in {
            "predicate": "identity.preferred_name",
            "subject_entity_key": "local:different-subject",
            "object_literal": "amber",
            "sensitivity": "sensitive_self",
            "surface": "explicit_only",
            "requires_explicit": True,
            "projectable": False,
            "domains": ["different_domain"],
            "intents": ["different_intent"],
        }.items():
            claim = copy.deepcopy(baseline)
            claim[field] = replacement
            cases.append((field, claim, command(claim, "retract")))
        cross_owner = command(baseline, "retract")
        cross_owner["actor"] = actor(owner=OWNER_B)
        cases.append(("owner", baseline, cross_owner))
        missing_scope = command(baseline, "retract")
        missing_scope["actor"] = actor(scopes=())
        cases.append(("scope", baseline, missing_scope))
        for name, claim, value in cases:
            with self.subTest(name=name), self.assertRaises(ContractViolation):
                transition(claim, value)

    def test_invalid_transitions_and_caller_clock_fail_closed(self) -> None:
        self.assertNotIn(
            "occurred_at", inspect.signature(apply_lifecycle_command).parameters
        )
        active = make_claim_row()
        caller_clock = command(active, "retract")
        caller_clock["occurred_at"] = NOW
        cases = (
            (active, caller_clock),
            (make_claim_row(lifecycle_state="retracted"), command(make_claim_row(lifecycle_state="retracted"), "correct")),
            (make_claim_row(lifecycle_state="retracted"), command(make_claim_row(lifecycle_state="retracted"), "retract")),
            (make_claim_row(lifecycle_state="deletion_pending"), command(make_claim_row(lifecycle_state="deletion_pending"), "correct")),
        )
        for claim, value in cases:
            with self.subTest(action=value["action"]), self.assertRaises(
                ContractViolation
            ):
                transition(claim, value)


if __name__ == "__main__":
    unittest.main()
