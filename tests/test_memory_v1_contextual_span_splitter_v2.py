from __future__ import annotations

import unittest

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    contextual_span_plan_v2,
    contextual_spans_v2,
)


def _assert_coverage(source: str) -> None:
    spans = contextual_spans_v2(source)
    previous_end = 0
    for span in spans:
        assert not source[previous_end:span.char_start].strip()
        assert source[span.char_start:span.char_end] == span.content
        assert len(span.content) <= 520
        previous_end = span.char_end
    assert not source[previous_end:].strip()


class ContextualSpanSplitterV2Test(unittest.TestCase):
    def test_preserves_unicode_source_offsets_and_whitespace_coverage(
        self,
    ) -> None:
        source = (
            "My cat Арктика slept beside me.  "
            "She was calm, and I cared for her."
        )
        _assert_coverage(source)
        spans = contextual_spans_v2(source)
        self.assertEqual(
            [span.content for span in spans],
            [
                "My cat Арктика slept beside me.",
                "She was calm, and I cared for her.",
            ],
        )
        self.assertTrue(spans[1].context_needed)

    def test_splits_long_speech_to_text_at_clause_boundaries(self) -> None:
        source = (
            "I worked with dogs for many years, and I traveled across Wisconsin "
            "to train them, and I built secure kennels into my vehicles, and I "
            "brought the dogs with me while I visited clinics, and I stopped "
            "regularly so they could exercise, and I kept the vehicle cooled "
            "while they waited, and I continued doing this throughout the period "
            "when the clinics were being built, and the dogs remained important "
            "companions during that work."
        )
        _assert_coverage(source)
        spans = contextual_spans_v2(source, max_span_chars=200)
        self.assertGreaterEqual(len(spans), 3)
        self.assertTrue(all(len(span.content) <= 200 for span in spans))
        self.assertTrue(
            any(
                span.boundary_reason == "clause_boundary"
                for span in spans
            )
        )

    def test_keeps_compact_family_list_together(self) -> None:
        source = (
            "I have three sisters: Cindy is older, Lori is younger, and Heidi is "
            "the youngest."
        )
        spans = contextual_spans_v2(source)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].content, source)

    def test_marks_short_anaphoric_temporal_fragment_as_context_needed(
        self,
    ) -> None:
        spans = contextual_spans_v2("That was when I was in my early 20s.")
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].context_needed)

    def test_question_is_preserved_for_terminal_question_policy(self) -> None:
        spans = contextual_spans_v2("What type of philosophy do I follow?")
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].content.endswith("?"))

    def test_public_plan_contains_only_typed_deterministic_fields(self) -> None:
        plan = contextual_span_plan_v2(
            "I was born in Green Bay, Wisconsin."
        )
        self.assertEqual(
            set(plan[0]),
            {
                "ordinal",
                "char_start",
                "char_end",
                "content",
                "content_sha256",
                "boundary_reason",
                "context_needed",
                "primary_lane",
                "epistemic_role",
                "span_origin",
            },
        )


if __name__ == "__main__":
    unittest.main()
