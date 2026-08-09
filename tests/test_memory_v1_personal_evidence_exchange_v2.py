from __future__ import annotations

import hashlib
import unittest

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    build_memory_evidence_context_envelope_v2,
)
from rag_engine.memory_v1_personal_evidence_exchange_v2 import (
    classify_personal_evidence_exchange_v2,
    content_free_disposition_receipt_v2,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    TRUSTED_SOURCE_ROLE,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
THREAD = "2f6e6a6c-f43e-45e2-8137-8b1b01e67976"
SOURCE = "ed91f3b9-a4b8-4e63-aac7-53e370412483"
TARGET = "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964"
REQUEST = "bfca3e63-e670-4601-a06d-6345c18554f4"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def context_for(
    target_text: str,
    assistant_question: str,
    *,
    preceding_user: str | None = None,
):
    source = {
        "id": SOURCE,
        "owner_user_id": OWNER,
        "thread_id": THREAD,
        "request_id": REQUEST,
        "created_at": "2026-08-07T12:00:00+00:00",
        "text": target_text,
    }
    evidence = [{
        "evidence_id": TARGET,
        "owner_user_id": OWNER,
        "source_system": "public.chat_log",
        "content": target_text,
        "content_sha256": sha(target_text),
        "metadata": {
            "source_id": SOURCE,
            "thread_id": THREAD,
            "request_id": REQUEST,
            "source_content_sha256": sha(target_text),
            "source_char_start": 0,
            "source_char_end": len(target_text),
            "primary_lane": "personal_history",
            "epistemic_role": "user_report",
            "span_origin": "atomic",
        },
    }]
    rows = []
    if preceding_user is not None:
        rows.append({
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "request_id": "request-1",
            "created_at": "2026-08-07T11:58:00+00:00",
            "source": "frontend/chat:user",
            "text": preceding_user,
        })
    rows.append({
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "owner_user_id": OWNER,
        "thread_id": THREAD,
        "request_id": "request-2",
        "created_at": "2026-08-07T11:59:00+00:00",
        "source": "frontend/chat:assistant",
        "text": assistant_question,
    })
    sibling = build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=OWNER,
        target_evidence_id=TARGET,
        expected_target_content_sha256=sha(target_text),
        source_row=source,
        evidence_rows=evidence,
    )
    return build_memory_evidence_context_envelope_v2(
        sibling_context=sibling,
        prior_turn_rows=rows,
    )


def classify(text: str, context=None):
    return classify_personal_evidence_exchange_v2(
        text,
        source_role=TRUSTED_SOURCE_ROLE,
        evidence_context=context,
    )


class PersonalEvidenceExchangeV2Test(unittest.TestCase):
    def test_high_recall_owner_authored_matrix(self) -> None:
        cases = (
            "Relational monism keeps showing up differently in my thinking.",
            "My childhood summers were mostly spent near the lake.",
            "The way this memory grows gradually matters a great deal to me.",
            "That approach has become harder for me to trust.",
            "Long-form answers are easier for me to use.",
            "We may move the project to Ohio next spring.",
            "I trust her.",
        )
        for text in cases:
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.decision, "send_external")
                self.assertEqual(
                    result.reason_codes,
                    ("high_recall_owner_authored_candidate",),
                )
                self.assertEqual(len(result.selected_spans), 1)
                span = result.selected_spans[0]
                self.assertEqual(
                    text[span.char_start:span.char_end],
                    text,
                )
                self.assertEqual(
                    span.category,
                    "open_personal_evidence_candidate",
                )
                self.assertEqual(span.subject_hint, "unknown")

    def test_high_confidence_exclusion_and_review_matrix(self) -> None:
        cases = (
            ("Authorized as written.", "skip_zero_call", "workflow_noise_zero_call"),
            ("Continue with authorization as written.", "skip_zero_call", "workflow_noise_zero_call"),
            ("Run the tests.", "skip_zero_call", "workflow_noise_zero_call"),
            ("Try again.", "skip_zero_call", "workflow_noise_zero_call"),
            ("That sounds good.", "skip_zero_call", "workflow_noise_zero_call"),
            ("Thanks.", "skip_zero_call", "social_only"),
            ("Chicago.", "review_context", "context_binding_required"),
            ("Eric said I prefer tea.", "review_context", "unadopted_quote"),
        )
        for text, decision, reason in cases:
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.decision, decision)
                self.assertEqual(result.reason_codes, (reason,))
                self.assertEqual(result.selected_spans, ())

    def test_disposition_receipt_is_content_free_and_deterministic(self) -> None:
        text = "My childhood summers were mostly spent near the lake."
        result = classify(text)
        receipt = content_free_disposition_receipt_v2(result)
        self.assertEqual(receipt, result.disposition_receipt())
        self.assertEqual(
            receipt["contract_version"],
            "memory_v1_personal_evidence_disposition_receipt_v1",
        )
        self.assertEqual(receipt["decision"], "send_external")
        self.assertEqual(receipt["selected_span_count"], 1)
        self.assertNotIn(text, repr(receipt))
        self.assertNotIn("childhood", repr(receipt).casefold())

    def test_high_recall_never_overrides_origin_or_sensitive_routing(self) -> None:
        assistant = classify_personal_evidence_exchange_v2(
            "My childhood summers were mostly spent near the lake.",
            source_role="frontend/chat:assistant",
        )
        self.assertEqual(assistant.decision, "review_context")
        self.assertEqual(assistant.reason_codes, ("non_user_role",))
        self.assertEqual(assistant.selected_spans, ())

        sensitive = classify("My father has dementia and lives in assisted living.")
        self.assertEqual(sensitive.decision, "route_internal")
        self.assertEqual(
            sensitive.reason_codes,
            ("sensitive_third_party_local_only",),
        )

    def test_obvious_information_and_advice_stay_zero_call(self) -> None:
        for text, reason in (
            (
                "Who won America's Next Top Model in 1964?",
                "pure_general_question",
            ),
            (
                "What's the best treatment for a swollen ankle?",
                "pure_advice_question",
            ),
        ):
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.decision, "skip_zero_call")
                self.assertEqual(result.reason_codes, (reason,))
                self.assertEqual(result.selected_spans, ())

    def test_indirect_durable_propositions_are_selected(self) -> None:
        cases = (
            (
                "Short answers work best for me.",
                "response_preference",
                "endorsed",
            ),
            (
                "Lately, relational monism makes more sense to me.",
                "belief_stance_idea",
                "endorsed",
            ),
            (
                "Next year, moving to Wisconsin is the goal.",
                "goal_plan_intention",
                "planned",
            ),
        )
        for text, category, mode in cases:
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.decision, "send_external")
                self.assertEqual(
                    result.reason_codes,
                    ("indirect_personal_proposition_selected",),
                )
                self.assertEqual(result.selected_spans[0].category, category)
                self.assertEqual(result.selected_spans[0].assertion_mode, mode)

    def test_embedded_question_selects_only_explicit_clause(self) -> None:
        text = "Does it make sense that I prefer short answers?"
        result = classify(text)
        self.assertEqual(result.decision, "send_external")
        span = result.selected_spans[0]
        self.assertEqual(text[span.char_start:span.char_end], "I prefer short answers")
        self.assertEqual(span.category, "response_preference")

    def test_embedded_sensitive_question_stays_local(self) -> None:
        text = "Is it normal that my father has dementia?"
        result = classify(text)
        self.assertEqual(result.decision, "route_internal")
        self.assertEqual(result.selected_spans[0].sensitivity, "sensitive_third_party")
        self.assertEqual(
            text[
                result.selected_spans[0].char_start:
                result.selected_spans[0].char_end
            ],
            "my father has dementia",
        )

    def test_short_answer_requires_bound_context(self) -> None:
        for text in ("Three.", "Chicago.", "Not Chicago—Milwaukee."):
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.decision, "review_context")
                self.assertEqual(
                    result.reason_codes,
                    ("context_binding_required",),
                )
                self.assertEqual(result.selected_spans, ())

    def test_bound_assistant_question_promotes_only_target_answer(self) -> None:
        cases = (
            ("Three.", "How many sisters do you have?", "family_relationship"),
            ("Chicago.", "Where did you live after college?", "residence_life_history"),
            ("Short paragraphs.", "How do you prefer responses?", "response_preference"),
        )
        for text, question, category in cases:
            with self.subTest(text=text):
                context = context_for(
                    text,
                    question,
                    preceding_user="Earlier user context is not assertion authority.",
                )
                result = classify(text, context)
                self.assertEqual(result.decision, "send_external")
                self.assertEqual(result.selected_spans[0].category, category)
                self.assertEqual(
                    text[
                        result.selected_spans[0].char_start:
                        result.selected_spans[0].char_end
                    ],
                    text,
                )
                self.assertEqual(
                    result.context_envelope_sha256,
                    context.envelope_sha256,
                )
                self.assertEqual(len(result.selected_spans), 1)

    def test_contextual_correction_is_distinct(self) -> None:
        text = "Not Chicago—Milwaukee."
        context = context_for(text, "Where does your sister live?")
        result = classify(text, context)
        self.assertEqual(result.decision, "send_external")
        self.assertEqual(
            result.reason_codes,
            ("contextual_correction_selected",),
        )
        self.assertEqual(result.selected_spans[0].assertion_mode, "corrective")

    def test_context_cannot_promote_nonanswer_or_general_answer(self) -> None:
        for text, question in (
            ("I don't know the answer.", "How many sisters do you have?"),
            ("Paris.", "What is the capital of France?"),
        ):
            with self.subTest(text=text):
                result = classify(text, context_for(text, question))
                self.assertNotEqual(result.decision, "send_external")
                self.assertEqual(result.selected_spans, ())

    def test_quoted_or_attributed_material_never_becomes_owner_claim(self) -> None:
        quoted = classify('She wrote that I prefer tea.')
        self.assertEqual(quoted.decision, "review_context")
        self.assertEqual(quoted.reason_codes, ("unadopted_quote",))
        self.assertEqual(quoted.selected_spans, ())

        attributed = classify('My sister said, "I live in Denver."')
        self.assertEqual(attributed.decision, "send_external")
        self.assertTrue(attributed.selected_spans)
        self.assertTrue(
            all(
                span.subject_hint == "attributed_third_party"
                for span in attributed.selected_spans
            )
        )

    def test_context_only_question_and_ambiguous_answer_are_not_promoted(
        self,
    ) -> None:
        context_only = classify("What did you mean by that?")
        self.assertEqual(context_only.decision, "skip_zero_call")
        self.assertEqual(context_only.selected_spans, ())

        ambiguous = classify(
            "Maybe.",
            context_for("Maybe.", "How many sisters do you have?"),
        )
        self.assertEqual(ambiguous.decision, "review_context")
        self.assertEqual(ambiguous.reason_codes, ("ambiguous_context_fragment",))
        self.assertEqual(ambiguous.selected_spans, ())

    def test_sensitive_contextual_answer_stays_local(self) -> None:
        text = "Yes."
        result = classify(
            text,
            context_for(text, "Does your father have dementia?"),
        )
        self.assertEqual(result.decision, "route_internal")
        self.assertEqual(result.selected_spans[0].sensitivity, "sensitive_third_party")

    def test_personal_question_without_explicit_clause_needs_context(self) -> None:
        result = classify("Should we build the memory system this way?")
        self.assertEqual(result.decision, "review_context")
        self.assertEqual(
            result.reason_codes,
            ("personal_question_needs_context",),
        )


if __name__ == "__main__":
    unittest.main()
