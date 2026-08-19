from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from seebx.capabilities.conversation.tagging import (
    infer_transcript_tags,
    infer_vb_tags,
    normalize_vb_source,
)


class ConversationTaggingTests(unittest.TestCase):
    def test_existing_verbal_behavior_order_and_source_filter_are_stable(self) -> None:
        text = "Please help me because I think this system is lazy but clear."
        self.assertEqual(
            infer_vb_tags(text, source="user"),
            [
                "vb_desire:explicit_request",
                "vb_ontology:high_abstraction",
                "vb_stance:hedged",
                "vb_relation:causal",
                "vb_relation:contrast",
                "vb_fiction:mentalistic_term",
            ],
        )
        self.assertEqual(
            infer_vb_tags(text, source="assistant"),
            [
                "vb_ontology:high_abstraction",
                "vb_stance:hedged",
                "vb_relation:causal",
                "vb_relation:contrast",
            ],
        )

    def test_source_normalization_preserves_frontend_user_semantics(self) -> None:
        self.assertEqual(normalize_vb_source("frontend/chat:user"), "user")
        with patch.dict(os.environ, {"VB_TAG_SOURCE_NORMALIZE": "1"}):
            self.assertIn(
                "vb_desire:explicit_request",
                infer_transcript_tags(
                    "Could you explain this?",
                    source="frontend/chat:user",
                ),
            )

    def test_transcript_heuristics_retain_order_and_labels(self) -> None:
        with patch.dict(os.environ, {"VB_TAG_SOURCE_NORMALIZE": "0"}):
            self.assertEqual(
                infer_transcript_tags(
                    "Write a prose summary and analyze my workout.",
                    source="frontend",
                ),
                [
                    "format:prose",
                    "topic:workout",
                    "intent:summarize",
                    "intent:analyze",
                    "intent:generate",
                ],
            )


if __name__ == "__main__":
    unittest.main()
