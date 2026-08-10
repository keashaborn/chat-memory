"""PostgreSQL revalidation tests for untrusted synthetic vector candidates."""

from __future__ import annotations

import copy
from datetime import timedelta
import inspect
import unittest

from rag_engine.governed_memory.admission import recompute_claim_state_sha256
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.retrieval import (
    RETRIEVAL_POLICY_DOMAIN,
    RetrievalPolicy,
    retrieval_policy_material_bytes,
    retrieval_policy_sha256,
    revalidate_candidates,
)
from tests.memory._fixtures import (
    CLAIM_A,
    FIXTURE_PROVENANCE,
    NOW,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    REVISION_B,
    make_candidate,
    make_claim_row,
)


def policy(
    *,
    explicit_recall: bool = False,
    domains: tuple[str, ...] = (),
    intents: tuple[str, ...] = (),
) -> RetrievalPolicy:
    return RetrievalPolicy(
        explicit_recall=explicit_recall,
        allowed_predicates=("preference.personal",),
        domains=domains,
        intents=intents,
        max_records=8,
    )


def revalidate(
    candidates: object,
    rows: object,
    *,
    explicit_recall: bool = False,
    domains: tuple[str, ...] = (),
    intents: tuple[str, ...] = (),
    authorization_at=NOW,
):
    return revalidate_candidates(
        OWNER_A,
        candidates,
        rows,
        policy=policy(
            explicit_recall=explicit_recall,
            domains=domains,
            intents=intents,
        ),
        predicate_catalog=PREDICATE_CATALOG,
        authorization_at=authorization_at,
    )


class RetrievalRevalidationTests(unittest.TestCase):
    def test_policy_hash_is_framed_content_only_and_has_a_frozen_vector(self) -> None:
        expected_material = (
            b"governed_memory.retrieval_policy.v1\n"
            b"explicit_recall:5:false\n"
            b"allowed_predicates_count:1:1\n"
            b"allowed_predicates_0:19:preference.personal\n"
            b"domains_count:1:0\n"
            b"intents_count:1:0\n"
            b"max_records:1:8\n"
            b"policy_revision:1:1\n"
        )
        self.assertEqual(RETRIEVAL_POLICY_DOMAIN, "governed_memory.retrieval_policy.v1")
        self.assertEqual(
            retrieval_policy_material_bytes(
                explicit_recall=False,
                allowed_predicates=("preference.personal",),
                domains=(),
                intents=(),
                max_records=8,
                policy_revision=1,
            ),
            expected_material,
        )
        expected_sha256 = (
            "293dfb7d7c4e6e7d6497028a3be3e36e9f5c0b50ce35a2fce9363ac29512400b"
        )
        self.assertEqual(policy().policy_sha256, expected_sha256)
        self.assertEqual(
            retrieval_policy_sha256(
                explicit_recall=False,
                allowed_predicates=("preference.personal",),
                domains=(),
                intents=(),
                max_records=8,
                policy_revision=1,
            ),
            expected_sha256,
        )

    def test_policy_excludes_time_and_authorization_time_is_required_per_use(self) -> None:
        self.assertNotIn("as_of", inspect.signature(RetrievalPolicy).parameters)
        authorization = inspect.signature(revalidate_candidates).parameters[
            "authorization_at"
        ]
        self.assertEqual(authorization.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(authorization.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            revalidate_candidates(
                OWNER_A,
                [make_candidate()],
                [make_claim_row()],
                policy=policy(),
                predicate_catalog=PREDICATE_CATALOG,
            )
        with self.assertRaisesRegex(
            ContractViolation, "invalid_retrieval_authorization_at"
        ):
            revalidate(
                [make_candidate()],
                [make_claim_row()],
                authorization_at=NOW.replace(tzinfo=None),
            )

    def test_authorization_time_controls_validity_without_changing_policy_hash(self) -> None:
        row = make_claim_row()
        row["valid_from"] = (NOW + timedelta(minutes=1)).isoformat()
        row["state_sha256"] = recompute_claim_state_sha256(row)
        frozen_policy_sha256 = policy().policy_sha256
        self.assertEqual(revalidate([make_candidate()], [row]), ())
        self.assertEqual(
            len(
                revalidate(
                    [make_candidate()],
                    [row],
                    authorization_at=NOW + timedelta(minutes=2),
                )
            ),
            1,
        )
        self.assertEqual(policy().policy_sha256, frozen_policy_sha256)

    def test_policy_domain_and_intent_overlap_is_fail_closed_when_both_are_scoped(self) -> None:
        row = make_claim_row()
        row["domains"] = ["personal"]
        row["intents"] = ["answer"]
        row["state_sha256"] = recompute_claim_state_sha256(row)
        self.assertEqual(len(revalidate([make_candidate()], [row])), 1)
        self.assertEqual(
            revalidate(
                [make_candidate()],
                [row],
                domains=("work",),
                intents=("answer",),
            ),
            (),
        )
        self.assertEqual(
            revalidate(
                [make_candidate()],
                [row],
                domains=("personal",),
                intents=("reflect",),
            ),
            (),
        )
        self.assertEqual(
            len(
                revalidate(
                    [make_candidate()],
                    [row],
                    domains=("personal",),
                    intents=("answer",),
                )
            ),
            1,
        )

    def test_exact_candidate_survives_postgres_revalidation(self) -> None:
        result = revalidate([make_candidate()], [make_claim_row()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["claim_id"], str(CLAIM_A))

    def test_cross_owner_candidate_and_row_never_survive(self) -> None:
        cases = (
            ([make_candidate(owner_user_id=str(OWNER_B))], [make_claim_row()]),
            ([make_candidate()], [make_claim_row(owner_user_id=OWNER_B)]),
        )
        for candidates, rows in cases:
            with self.subTest(candidates=candidates, rows=rows), self.assertRaises(
                ContractViolation
            ):
                revalidate(candidates, rows)

    def test_wrong_owner_candidate_hard_fails_before_other_malformed_fields(self) -> None:
        malformed = make_candidate(
            owner_user_id=str(OWNER_B),
            claim_id="not-a-uuid",
            revision_id="also-not-a-uuid",
            revision_number=True,
            score=float("nan"),
        )
        with self.assertRaisesRegex(ContractViolation, "cross_owner_vector_candidate"):
            revalidate([malformed], [make_claim_row()])

    def test_stale_revision_selection_and_number_never_survive(self) -> None:
        candidates = (
            make_candidate(revision_id=str(REVISION_B)),
            make_candidate(selection_binding_sha256="0" * 64),
            make_candidate(revision_number=2),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(revalidate([candidate], [make_claim_row()]), ())

    def test_noncurrent_nonactive_or_nonprojectable_rows_never_survive(self) -> None:
        not_projectable = make_claim_row()
        not_projectable["projectable"] = False
        rows = (
            make_claim_row(current=False),
            make_claim_row(lifecycle_state="retracted"),
            make_claim_row(lifecycle_state="correction_pending"),
            not_projectable,
        )
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(revalidate([make_candidate()], [row]), ())

    def test_malformed_authoritative_state_fails_closed(self) -> None:
        row = make_claim_row()
        row["epistemic_state"] = "unreviewed"
        with self.assertRaises(ContractViolation):
            revalidate([make_candidate()], [row])

    def test_sensitive_claims_require_explicit_retrieval_policy(self) -> None:
        for sensitivity in ("sensitive_self",):
            row = make_claim_row()
            row["sensitivity"] = sensitivity
            row["surface"] = "explicit_only"
            row["requires_explicit"] = True
            with self.subTest(sensitivity=sensitivity):
                self.assertEqual(revalidate([make_candidate()], [row]), ())
                explicit = revalidate(
                    [make_candidate()],
                    [row],
                    explicit_recall=True,
                )
                self.assertEqual(len(explicit), 1)
                self.assertEqual(explicit[0]["sensitivity"], sensitivity)

    def test_explicit_only_policy_is_closed_for_every_sensitivity(self) -> None:
        malformed_rows: list[dict[str, object]] = []
        explicit_without_flag = make_claim_row()
        explicit_without_flag["surface"] = "explicit_only"
        malformed_rows.append(explicit_without_flag)
        flag_without_surface = make_claim_row()
        flag_without_surface["requires_explicit"] = True
        malformed_rows.append(flag_without_surface)
        for row in malformed_rows:
            with self.subTest(row=row), self.assertRaises(ContractViolation):
                revalidate([make_candidate()], [row], explicit_recall=True)

    def test_authoritative_predicate_domain_intent_and_shape_corruption_is_denied(self) -> None:
        malformed_rows: list[dict[str, object]] = []
        for field, value in (
            ("predicate", "Not Canonical"),
            ("domains", ["zeta", "alpha"]),
            ("domains", ["duplicate", "duplicate"]),
            ("intents", "not-an-array"),
            ("subject_entity_type", "unsupported"),
            ("object_kind", "unsupported"),
        ):
            row = make_claim_row()
            row[field] = value
            malformed_rows.append(row)
        for row in malformed_rows:
            with self.subTest(row=row), self.assertRaises(ContractViolation):
                revalidate([make_candidate()], [row])

    def test_restricted_identifier_is_never_retrieved(self) -> None:
        row = make_claim_row()
        row["sensitivity"] = "restricted_identifier"
        row["surface"] = "never"
        row["requires_explicit"] = True
        row["projectable"] = False
        with self.assertRaises(ContractViolation):
            revalidate([make_candidate()], [row], explicit_recall=True)

    def test_candidate_payload_cannot_override_postgres_content_or_policy(self) -> None:
        malicious = make_candidate(predicate="attacker_predicate", object="attacker")
        with self.assertRaisesRegex(ContractViolation, "invalid_vector_candidate"):
            revalidate([malicious], [make_claim_row()])

    def test_duplicate_stable_candidate_identity_fails_closed(self) -> None:
        with self.assertRaises(ContractViolation):
            revalidate(
                [make_candidate(score=0.91), make_candidate(score=0.89)],
                [make_claim_row()],
            )

    def test_unknown_candidate_has_no_fallback(self) -> None:
        unknown = make_candidate(claim_id="00000000-0000-4000-8000-000000000000")
        self.assertEqual(revalidate([unknown], [make_claim_row()]), ())

    def test_candidate_and_authoritative_rows_are_bounded_to_eight(self) -> None:
        for candidates, rows in (
            ([make_candidate()] * 9, [make_claim_row()]),
            ([make_candidate()], [make_claim_row()] * 9),
        ):
            with self.subTest(candidate_count=len(candidates), row_count=len(rows)), self.assertRaises(
                ContractViolation
            ):
                revalidate(candidates, rows)

    def test_normalized_dot_score_must_be_within_closed_unit_interval(self) -> None:
        for score in (-1.000000001, 1.000000001, 10**1000):
            with self.subTest(score=score), self.assertRaises(ContractViolation):
                revalidate([make_candidate(score=score)], [make_claim_row()])
        for score in (-1.0, 1.0):
            with self.subTest(score=score):
                result = revalidate([make_candidate(score=score)], [make_claim_row()])
                self.assertEqual(result[0]["score"], score)

    def test_revalidation_does_not_mutate_inputs(self) -> None:
        candidates = [make_candidate()]
        rows = [make_claim_row()]
        before_candidates = copy.deepcopy(candidates)
        before_rows = copy.deepcopy(rows)
        revalidate(candidates, rows)
        self.assertEqual(candidates, before_candidates)
        self.assertEqual(rows, before_rows)


if __name__ == "__main__":
    unittest.main()
