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
