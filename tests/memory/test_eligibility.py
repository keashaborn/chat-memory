"""Pure deterministic eligibility tests using synthetic messages only."""

from __future__ import annotations

import json
import unittest

from rag_engine.governed_memory.contracts import EligibilityDecision
from rag_engine.governed_memory.eligibility import evaluate_eligibility
from tests.memory._fixtures import (
    CUTOVER,
    EXCHANGE_A,
    FIXTURE_PROVENANCE,
    SOURCE_TEXT,
    WINDOW_A,
    make_ingest_payload,
)


class EligibilityTests(unittest.TestCase):
    def evaluate(self, text: str, **overrides: object) -> dict[str, object]:
        payload = make_ingest_payload(text=text)
        payload.update(overrides)
        return evaluate_eligibility(payload, cutover=CUTOVER)

    def test_all_five_outcomes_remain_distinct(self) -> None:
        scenarios = {
            EligibilityDecision.SEND_EXTERNAL: SOURCE_TEXT,
            EligibilityDecision.SKIP_ZERO_CALL: "What is a binary tree?",
            EligibilityDecision.ROUTE_INTERNAL: "Log 5 synthetic repetitions for the current workout.",
            EligibilityDecision.BLOCK_LOCAL: "My password is SYNTHETIC_SECRET_DO_NOT_STORE.",
            EligibilityDecision.REVIEW_CONTEXT: "Yes, that synthetic preference is still current.",
        }
        observed = {
            decision: self.evaluate(text)["decision"]
            for decision, text in scenarios.items()
        }
        self.assertEqual(observed, {decision: decision.value for decision in scenarios})

    def test_only_send_external_selects_evidence(self) -> None:
        selected = self.evaluate(SOURCE_TEXT)
        self.assertEqual(selected["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertIsNotNone(selected["selected_evidence"])
        self.assertTrue(selected["provider_allowed"])

        for text in (
            "What is a binary tree?",
            "Log 5 synthetic repetitions for the current workout.",
            "My password is SYNTHETIC_SECRET_DO_NOT_STORE.",
            "Yes, that synthetic preference is still current.",
        ):
            with self.subTest(text=text):
                result = self.evaluate(text)
                self.assertIsNone(result["selected_evidence"])
                self.assertFalse(result["provider_allowed"])

    def test_disposition_receipt_preserves_lineage_without_content(self) -> None:
        result = self.evaluate("What is a binary tree?")
        receipt = result["receipt"]
        self.assertEqual(receipt["exchange_id"], str(EXCHANGE_A))
        self.assertEqual(receipt["window_id"], str(WINDOW_A))
        self.assertEqual(receipt["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("binary tree", serialized)
        self.assertNotIn("content", receipt)
        self.assertNotIn("source_sha256", receipt)
        self.assertNotIn("content_sha256", receipt)
        self.assertNotIn(make_ingest_payload(text="What is a binary tree?")["content_sha256"], serialized)

    def test_selected_utf8_offsets_and_hash_are_source_exact(self) -> None:
        text = "Synthetic owner prefers the cobalt theme ☕."
        result = self.evaluate(text)
        evidence = result["selected_evidence"]
        encoded = text.encode("utf-8")
        self.assertEqual(evidence["start_utf8"], 0)
        self.assertEqual(evidence["end_utf8"], len(encoded))
        self.assertEqual(evidence["selected_text"].encode("utf-8"), encoded)
        self.assertEqual(evidence["source_sha256"], make_ingest_payload(text=text)["content_sha256"])
        self.assertEqual(evidence["selected_sha256"], evidence["source_sha256"])

    def test_preferred_noun_phrase_is_selected_as_life_preference(self) -> None:
        result = self.evaluate("My preferred test color is cobalt blue.")
        evidence = result["selected_evidence"]

        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["category"], "life_preference")
        self.assertEqual(evidence["assertion_mode"], "endorsed")

    def test_explicit_correction_command_never_enters_ordinary_extraction(self) -> None:
        result = self.evaluate(
            "My preferred correction test fruit is now a kiwi, "
            "replacing the mango."
        )

        self.assertEqual(
            result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value
        )
        self.assertEqual(
            result["reason_codes"], ["explicit_correction_command"]
        )
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_clear_owner_declaration_is_candidate_without_remember_command(self) -> None:
        result = self.evaluate("I have a dog named Pepper.")
        evidence = result["selected_evidence"]

        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertEqual(result["reason_codes"], ["owner_declaration_selected"])
        self.assertEqual(evidence["selected_text"], "I have a dog named Pepper.")
        self.assertEqual(evidence["category"], "pet")
        self.assertEqual(evidence["assertion_mode"], "asserted")

    def test_qualified_owner_declaration_is_sent_as_uncertain_evidence(self) -> None:
        text = (
            "Marimba is a possible future option for memory validation, while "
            "vibraphone remains my current preferred instrument."
        )
        result = self.evaluate(text)
        evidence = result["selected_evidence"]

        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertEqual(result["reason_codes"], ["owner_declaration_selected"])
        self.assertEqual(evidence["selected_text"], text)
        self.assertEqual(evidence["category"], "life_preference")
        self.assertEqual(evidence["assertion_mode"], "uncertain")

    def test_explicit_project_deliberation_is_selected_as_owner_evidence(self) -> None:
        text = (
            "For my memory project, I am considering a local model for private "
            "summaries. Another direction I am exploring is a smaller hosted "
            "model for ordinary claims."
        )
        result = self.evaluate(text)
        evidence = result["selected_evidence"]

        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertEqual(result["reason_codes"], ["owner_declaration_selected"])
        self.assertEqual(evidence["selected_text"], text)
        self.assertEqual(evidence["assertion_mode"], "uncertain")

    def test_project_work_remains_excluded_without_owner_deliberation(self) -> None:
        result = self.evaluate(
            "I am implementing a new retrieval route in my memory project."
        )

        self.assertEqual(result["decision"], EligibilityDecision.ROUTE_INTERNAL.value)
        self.assertEqual(result["reason_codes"], ["project_scope_excluded"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_considered_direction_question_remains_zero_call(self) -> None:
        result = self.evaluate(
            "What possible project directions am I considering?"
        )

        self.assertEqual(result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        self.assertEqual(result["reason_codes"], ["pure_question"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_project_deliberation_memory_denial_remains_zero_call(self) -> None:
        result = self.evaluate(
            "Do not remember that I am considering a hosted model for my project."
        )

        self.assertEqual(result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        self.assertEqual(result["reason_codes"], ["no_durable_fact"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_remember_is_optional_emphasis_not_required_syntax(self) -> None:
        text = (
            "Remember that marimba is a possible future option for memory "
            "validation, while vibraphone remains my current preferred instrument."
        )
        result = self.evaluate(text)
        evidence = result["selected_evidence"]

        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertEqual(result["reason_codes"], ["explicit_remember_selected"])
        self.assertEqual(
            evidence["selected_text"],
            "marimba is a possible future option for memory validation, while "
            "vibraphone remains my current preferred instrument.",
        )
        self.assertEqual(evidence["assertion_mode"], "uncertain")

    def test_memory_denial_stays_zero_call_even_with_personal_fact(self) -> None:
        variants = (
            "Do not remember that my preferred tea is Earl Grey.",
            "A marimba could be another option someday, but do not change my "
            "saved memory validation instrument.",
        )
        for text in variants:
            with self.subTest(text=text):
                result = self.evaluate(text)
                self.assertEqual(
                    result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value
                )
                self.assertEqual(result["reason_codes"], ["no_durable_fact"])
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["selected_evidence"])

    def test_general_remember_request_does_not_become_personal_memory(self) -> None:
        result = self.evaluate("Remember that Saturn has rings.")

        self.assertEqual(result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_first_person_task_does_not_become_personal_memory(self) -> None:
        result = self.evaluate("I want you to explain how a binary tree works.")

        self.assertEqual(result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        self.assertEqual(result["reason_codes"], ["task_request"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_first_person_workflow_noise_does_not_call_provider(self) -> None:
        for text in ("I agree with that.", "I understand.", "I don't know."):
            with self.subTest(text=text):
                result = self.evaluate(text)
                self.assertEqual(
                    result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value
                )
                self.assertEqual(result["reason_codes"], ["workflow_control"])
                self.assertFalse(result["provider_allowed"])

    def test_broader_hypothetical_does_not_call_provider(self) -> None:
        result = self.evaluate(
            "If I started preferring tea someday, would that become memory?"
        )

        self.assertEqual(result["decision"], EligibilityDecision.SKIP_ZERO_CALL.value)
        self.assertEqual(result["reason_codes"], ["no_durable_fact"])
        self.assertFalse(result["provider_allowed"])

    def test_non_user_role_fails_closed(self) -> None:
        result = self.evaluate(SOURCE_TEXT, role="assistant")
        self.assertEqual(result["decision"], EligibilityDecision.BLOCK_LOCAL.value)
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])

    def test_sensitive_and_restricted_identifier_text_never_reaches_provider(self) -> None:
        scenarios = (
            (
                "I was diagnosed with synthetic diabetes.",
                EligibilityDecision.ROUTE_INTERNAL.value,
                "sensitive_local_only",
            ),
            (
                "My email is synthetic.owner@example.invalid.",
                EligibilityDecision.BLOCK_LOCAL.value,
                "direct_identifier",
            ),
        )
        for text, decision, reason in scenarios:
            with self.subTest(reason=reason):
                result = self.evaluate(text)
                self.assertEqual(result["decision"], decision)
                self.assertEqual(result["reason_codes"], [reason])
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)
                serialized = json.dumps(result["receipt"], sort_keys=True)
                self.assertNotIn("source_sha256", result["receipt"])
                self.assertNotIn("content_sha256", result["receipt"])
                self.assertNotIn(make_ingest_payload(text=text)["content_sha256"], serialized)

    def test_secret_financial_and_high_entropy_variants_never_send_external(self) -> None:
        variants = (
            "Please remember that my private key is SYNTHETIC_PRIVATE_KEY_123.",
            "Remember that my recovery token is " + "A7" * 32 + ".",
            "Remember that my bank account number is 123456789.",
            "Remember that my routing number is 021000021.",
            "Remember that my brokerage account is SYNTHETIC-ACCT-884422.",
            "Remember that my card number is 4111 1111 1111 1111.",
        )
        for text in variants:
            with self.subTest(text=text):
                result = self.evaluate(text)
                self.assertNotEqual(
                    result["decision"],
                    EligibilityDecision.SEND_EXTERNAL.value,
                    "an explicit remember prefix must not bypass secret filtering",
                )
                self.assertFalse(result["provider_allowed"])
                self.assertIsNone(result["selected_evidence"])
                self.assertEqual(result["receipt"]["external_model_calls"], 0)
                serialized = json.dumps(result["receipt"], sort_keys=True)
                self.assertNotIn(text, serialized)
                self.assertNotIn(
                    make_ingest_payload(text=text)["content_sha256"],
                    serialized,
                )


if __name__ == "__main__":
    unittest.main()
