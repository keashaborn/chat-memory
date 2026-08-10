"""Pure explicit-review and admission transition tests."""

from __future__ import annotations

import copy
from datetime import timedelta
import inspect
import json
import unittest

from rag_engine.governed_memory.admission import (
    AUTHORITATIVE_PROPOSAL_RECORD_FIELDS,
    apply_review,
    expire_proposal,
)
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.contracts import ContractViolation, OperationOutcome
from tests.memory._fixtures import (
    NOW,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    SOURCE_TEXT,
    SYNTHETIC_PREDICATE_CATALOG_SHA256,
    make_proposal,
)


def actor(*, owner=OWNER_A, actor_id=OWNER_A, scopes=(ActorScope.REVIEW_PROPOSALS,)) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=owner,
        actor_id=actor_id,
        role=ActorRole.OWNER,
        scopes=scopes,
        authentication_manifest_sha256="a" * 64,
        authenticated_at=NOW,
    )


def command(
    proposal: dict[str, object] | None = None,
    *,
    decision: str = "admit",
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    proposal = make_proposal() if proposal is None else proposal
    return {
        "actor": actor(),
        "proposal_id": proposal["proposal_id"],
        "operation_id": proposal["operation_id"],
        "decision": decision,
        "expected_proposal_sha256": proposal["proposal_sha256"],
        "expected_source_sha256": proposal["source_sha256"],
        "expected_selected_sha256": proposal["selected_sha256"],
        "expected_selection_binding_sha256": proposal[
            "selection_binding_sha256"
        ],
        "expected_predicate_catalog_sha256": proposal[
            "predicate_catalog_sha256"
        ],
        "reason_codes": (
            ["explicit_owner_review"]
            if decision == "admit"
            else ["proposal_incorrect"]
        )
        if reason_codes is None
        else reason_codes,
    }


def review(
    proposal: dict[str, object],
    value: dict[str, object] | None = None,
    *,
    transaction_time=NOW,
):
    return apply_review(
        proposal,
        command(proposal) if value is None else value,
        predicate_catalog=PREDICATE_CATALOG,
        transaction_time=transaction_time,
    )


class AdmissionTests(unittest.TestCase):
    def test_operation_outcome_contract_has_no_unused_noop_state(self) -> None:
        self.assertEqual(
            tuple((outcome.name, outcome.value) for outcome in OperationOutcome),
            (("APPLIED", "applied"), ("REPLAYED", "replayed")),
        )

    def test_admission_emits_one_ordered_atomic_effect_plan(self) -> None:
        proposal = make_proposal()
        result = review(proposal)
        self.assertEqual(result["proposal_state"], "admitted")
        self.assertEqual(result["operation_outcome"], OperationOutcome.APPLIED.value)
        self.assertEqual(
            result["effects"],
            [
                "append_review_receipt",
                "create_claim",
                "create_claim_revision",
                "link_claim_evidence",
                "enqueue_projection",
            ],
        )
        self.assertIsNotNone(result["claim"])
        self.assertIsNotNone(result["revision"])
        self.assertIsNotNone(result["projection"])

    def test_rejection_is_terminal_without_claim_or_projection(self) -> None:
        proposal = make_proposal()
        result = review(proposal, command(proposal, decision="reject"))
        self.assertEqual(result["proposal_state"], "rejected")
        self.assertEqual(result["effects"], ["append_review_receipt"])
        self.assertIsNone(result["claim"])
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["projection"])

    def test_cross_owner_actor_is_rejected(self) -> None:
        cross_owner = command()
        cross_owner["actor"] = actor(owner=OWNER_B, actor_id=OWNER_B)
        with self.assertRaises(ContractViolation):
            review(make_proposal(), cross_owner)

    def test_missing_review_scope_is_rejected(self) -> None:
        unauthorized = command()
        unauthorized["actor"] = actor(scopes=())
        with self.assertRaises(ContractViolation):
            review(make_proposal(), unauthorized)

    def test_proposal_and_evidence_hash_mismatches_fail_closed(self) -> None:
        for field in (
            "expected_proposal_sha256",
            "expected_source_sha256",
            "expected_selected_sha256",
            "expected_selection_binding_sha256",
            "expected_predicate_catalog_sha256",
        ):
            malformed = command()
            malformed[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                review(make_proposal(), malformed)

    def test_only_pending_proposals_can_be_reviewed(self) -> None:
        for state in ("admitted", "rejected", "expired"):
            with self.subTest(state=state), self.assertRaises(ContractViolation):
                proposal = make_proposal(state=state)
                review(proposal, command(proposal))

    def test_reason_codes_are_sorted_unique_and_closed(self) -> None:
        proposal = make_proposal()
        malformed = (
            command(
                proposal,
                reason_codes=["explicit_owner_review", "explicit_owner_review"],
            ),
            command(proposal, reason_codes=["not a canonical reason"]),
            command(proposal, reason_codes=[]),
            command(proposal, reason_codes=["proposal_incorrect"]),
            command(
                proposal,
                decision="reject",
                reason_codes=["explicit_owner_review"],
            ),
            command(
                proposal,
                decision="reject",
                reason_codes=["proposal_incorrect", "duplicate_existing"],
            ),
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ContractViolation):
                review(proposal, value)

        for reasons in (
            ["duplicate_existing"],
            ["not_durable"],
            ["proposal_incorrect"],
            ["duplicate_existing", "not_durable", "proposal_incorrect"],
        ):
            with self.subTest(reasons=reasons):
                result = review(
                    proposal,
                    command(proposal, decision="reject", reason_codes=reasons),
                )
                self.assertEqual(result["proposal_state"], "rejected")

    def test_receipt_is_content_free_and_operation_bound(self) -> None:
        proposal = make_proposal()
        result = review(proposal)
        receipt = result["receipt"]
        serialized = json.dumps(receipt, sort_keys=True, default=str)
        self.assertNotIn(SOURCE_TEXT, serialized)
        self.assertNotIn("selected_text", receipt)
        self.assertEqual(receipt["operation_id"], proposal["operation_id"])
        self.assertEqual(
            receipt["predicate_catalog_sha256"],
            SYNTHETIC_PREDICATE_CATALOG_SHA256,
        )
        self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_restricted_identifier_admission_is_prohibited(self) -> None:
        proposal = make_proposal()
        proposal["sensitivity"] = "restricted_identifier"
        with self.assertRaises(ContractViolation):
            review(proposal, command(proposal))

    def test_sensitive_admission_is_explicit_only(self) -> None:
        for sensitivity in ("sensitive_self", "sensitive_third_party"):
            proposal = make_proposal()
            proposal["sensitivity"] = sensitivity
            with self.subTest(sensitivity=sensitivity), self.assertRaises(
                ContractViolation
            ):
                review(proposal, command(proposal))

    def test_proposal_fact_and_lineage_tampering_with_retained_hash_is_denied(self) -> None:
        mutations = {
            "subject_entity_key": lambda row: row.__setitem__(
                "subject_entity_key", "local:synthetic-person"
            ),
            "predicate": lambda row: row.__setitem__(
                "predicate", "identity.preferred_name"
            ),
            "object_literal": lambda row: row.__setitem__(
                "object_literal", "amber"
            ),
            "epistemic_state": lambda row: row.__setitem__(
                "epistemic_state", "uncertain"
            ),
            "job_id": lambda row: row.__setitem__(
                "job_id", "00000000-0000-4000-8000-000000000011"
            ),
            "evidence_id": lambda row: row.__setitem__(
                "evidence_id", "00000000-0000-4000-8000-000000000012"
            ),
            "provider_call_id": lambda row: row.__setitem__(
                "provider_call_id", "00000000-0000-4000-8000-000000000013"
            ),
        }
        baseline = make_proposal()
        for field, mutate in mutations.items():
            proposal = copy.deepcopy(baseline)
            mutate(proposal)
            self.assertEqual(
                proposal["proposal_sha256"],
                baseline["proposal_sha256"],
                "the adversarial fixture must retain the original proposal hash",
            )
            with self.subTest(field=field), self.assertRaises(ContractViolation):
                review(proposal, command(proposal))

    def test_authoritative_proposal_shape_and_trusted_clock_are_closed(self) -> None:
        proposal = make_proposal()
        self.assertEqual(
            tuple(sorted(proposal)),
            AUTHORITATIVE_PROPOSAL_RECORD_FIELDS,
        )
        self.assertNotIn("reviewed_at", inspect.signature(apply_review).parameters)
        forged = command(proposal)
        forged["reviewed_at"] = NOW
        with self.assertRaises(ContractViolation):
            review(proposal, forged)

    def test_review_expiry_is_exact_and_uses_only_trusted_transaction_time(self) -> None:
        proposal = make_proposal()
        expires_at = proposal["expires_at"]
        review(proposal, transaction_time=expires_at - timedelta(microseconds=1))
        with self.assertRaises(ContractViolation):
            review(proposal, transaction_time=expires_at)

    def test_new_claim_expiration_is_symmetric_and_content_free(self) -> None:
        proposal = make_proposal()
        expires_at = proposal["expires_at"]
        with self.assertRaises(ContractViolation):
            expire_proposal(
                proposal,
                predicate_catalog=PREDICATE_CATALOG,
                transaction_time=expires_at - timedelta(microseconds=1),
            )
        result = expire_proposal(
            proposal,
            predicate_catalog=PREDICATE_CATALOG,
            transaction_time=expires_at,
        )
        self.assertEqual(result["proposal_state"], "expired")
        self.assertEqual(
            result["effects"],
            ["append_proposal_expiration_receipt"],
        )
        self.assertIsNone(result["claim"])
        self.assertIsNone(result["revision"])
        self.assertIsNone(result["projection"])
        self.assertIsNone(result["correction_recovery"])
        serialized = json.dumps(result["receipt"], sort_keys=True, default=str)
        self.assertNotIn(SOURCE_TEXT, serialized)
        self.assertNotIn("selected_text", serialized)

    def test_review_and_expiration_require_the_exact_catalog(self) -> None:
        proposal = make_proposal()
        for operation in (
            lambda: apply_review(
                proposal,
                command(proposal),
                predicate_catalog={},
                transaction_time=NOW,
            ),
            lambda: expire_proposal(
                proposal,
                predicate_catalog={},
                transaction_time=proposal["expires_at"],
            ),
        ):
            with self.subTest(operation=operation), self.assertRaises(
                ContractViolation
            ):
                operation()


if __name__ == "__main__":
    unittest.main()
