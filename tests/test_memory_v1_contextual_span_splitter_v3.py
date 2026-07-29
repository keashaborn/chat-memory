from __future__ import annotations

import unittest

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    SPLITTER_VERSION,
    contextual_spans_v2,
)


class ContextualSpanSplitterV3Test(unittest.TestCase):
    def test_bare_pet_name_list_stays_with_antecedent(self) -> None:
        text = (
            "We also have two of her sisters that we got about six months "
            "apart; they are both half sisters. Bella and Beauty."
        )
        spans = contextual_spans_v2(text)
        self.assertEqual(
            SPLITTER_VERSION,
            "memory_v1_contextual_span_splitter_20260728_v3",
        )
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].content, text)
        self.assertEqual(
            spans[0].boundary_reason,
            "terminal_span",
        )
        self.assertFalse(spans[0].context_needed)
        self.assertEqual(spans[0].span_origin, "contextual_split_v3")

    def test_unrelated_name_list_remains_separate(self) -> None:
        text = "I visited the lake yesterday. Bella and Beauty."
        spans = contextual_spans_v2(text)
        self.assertEqual(
            [item.content for item in spans],
            [
                "I visited the lake yesterday.",
                "Bella and Beauty.",
            ],
        )
        self.assertTrue(spans[1].context_needed)
        self.assertEqual(
            {item.span_origin for item in spans},
            {"contextual_split_v3"},
        )


if __name__ == "__main__":
    unittest.main()
