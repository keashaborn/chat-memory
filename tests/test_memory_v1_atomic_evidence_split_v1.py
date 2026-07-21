from __future__ import annotations

import unittest

from scripts.memory_v1_atomic_evidence_split_v1 import sentence_ranges, spans


class AtomicEvidenceSplitV1Test(unittest.TestCase):
    def test_family_turn_splits_with_exact_coverage(self) -> None:
        text = (
            "My mother died back in March. She was about 87 years old. "
            "My father has Alzheimer’s. He is 90 years old. "
            "Besides that I have three sisters, Cindy, Lori, and Heidi."
        )
        values = spans(text)
        self.assertEqual(len(values), 5)
        previous = 0
        for ordinal, item in enumerate(values):
            self.assertEqual(item["ordinal"], ordinal)
            self.assertFalse(text[previous:item["char_start"]].strip())
            self.assertEqual(
                text[item["char_start"]:item["char_end"]], item["content"]
            )
            previous = item["char_end"]
        self.assertFalse(text[previous:].strip())

    def test_abbreviation_does_not_split(self) -> None:
        text = "Dr. Lund lives nearby. He is my brother."
        self.assertEqual(len(sentence_ranges(text)), 2)

    def test_unpunctuated_terminal_span_is_preserved(self) -> None:
        text = "My mother died in March"
        values = spans(text)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["boundary_reason"], "terminal_span")


if __name__ == "__main__":
    unittest.main()
