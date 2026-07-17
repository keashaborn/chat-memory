from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.memory_v1_predicate_entailment_v5_1 import (
    POLICY_VERSION,
    assess_observation_entailment,
)

ROOT = Path(__file__).resolve().parents[1]


def observation(
    text: str,
    quote: str,
    *,
    predicate: str = "occupation.works_as",
    polarity: str = "affirmed",
) -> dict:
    start = text.index(quote)
    return {
        "predicate": predicate,
        "polarity": polarity,
        "source_spans": [
            {"start": start, "end": start + len(quote), "quote": quote}
        ],
    }


class PredicateEntailmentV51Test(unittest.TestCase):
    def test_checked_in_policy_and_override_are_bound(self) -> None:
        policy = json.loads(
            (ROOT / "specs/memory_v1_predicate_entailment_v5_1.json").read_text()
        )
        override = json.loads(
            (
                ROOT
                / "evals/memory_v1_predicate_entailment_v5_1_overrides.jsonl"
            ).read_text()
        )
        self.assertEqual(policy["contract_version"], POLICY_VERSION)
        self.assertFalse(policy["runtime_active"])
        self.assertEqual(override["case_id"], "v5-04")
        self.assertEqual(
            override["entailment_policy_version"],
            POLICY_VERSION,
        )
        self.assertEqual(
            override["expected"]["forbidden_predicates"],
            ["occupation.works_as"],
        )

    def test_policy_version_is_explicit(self) -> None:
        self.assertEqual(POLICY_VERSION, "memory_v1_predicate_entailment_v5_1")

    def test_explicit_employment_accepts_occupation(self) -> None:
        text = "I work as a personal trainer and see clients three days a week."
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "accept")

    def test_current_role_identity_accepts_occupation(self) -> None:
        text = "I'm a personal trainer."
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "accept")

    def test_nonemployment_contradicts_affirmed_occupation(self) -> None:
        text = (
            "I became a personal trainer in education anyway I don’t actually "
            "do it for a living I think through NASM."
        )
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "source_contradicts_predicate")
        self.assertIn("don’t actually do it for a living", decision.source_span["quote"])

    def test_training_only_does_not_become_occupation(self) -> None:
        text = "I completed personal trainer education last year."
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "predicate_semantics_unresolved")

    def test_became_role_without_work_context_is_deferred(self) -> None:
        text = "I became a personal trainer."
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "predicate_semantics_unresolved")

    def test_explicit_negated_occupation_accepts_negated_observation(self) -> None:
        text = "I do not work as a personal trainer."
        decision = assess_observation_entailment(
            observation(text, "personal trainer", polarity="negated"), text
        )
        self.assertEqual(decision.status, "accept")

    def test_negated_observation_without_source_negation_is_deferred(self) -> None:
        text = "I work as a personal trainer."
        decision = assess_observation_entailment(
            observation(text, "personal trainer", polarity="negated"), text
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "predicate_semantics_unresolved")

    def test_unrelated_negation_does_not_block_explicit_employment(self) -> None:
        text = "I don't drink alcohol, and I work as a personal trainer."
        decision = assess_observation_entailment(
            observation(text, "personal trainer"), text
        )
        self.assertEqual(decision.status, "accept")

    def test_explicit_credential_accepts_credential_predicate(self) -> None:
        text = "I am certified as a personal trainer through NASM."
        decision = assess_observation_entailment(
            observation(
                text,
                "certified as a personal trainer",
                predicate="credential.reported",
            ),
            text,
        )
        self.assertEqual(decision.status, "accept")

    def test_ambiguous_credential_language_is_deferred(self) -> None:
        text = "I became a personal trainer in education anyway."
        decision = assess_observation_entailment(
            observation(
                text,
                "personal trainer",
                predicate="credential.reported",
            ),
            text,
        )
        self.assertEqual(decision.status, "defer")
        self.assertEqual(decision.reason_code, "predicate_semantics_unresolved")

    def test_other_predicates_remain_structurally_governed(self) -> None:
        text = "My dog's name is Koda."
        decision = assess_observation_entailment(
            observation(
                text,
                "Koda",
                predicate="identity.name",
            ),
            text,
        )
        self.assertEqual(decision.status, "accept")


if __name__ == "__main__":
    unittest.main()
