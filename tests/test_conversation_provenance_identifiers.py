from __future__ import annotations

import unittest
from pathlib import Path

from seebx.contracts.conversation_provenance import (
    ACCEPTED_ASSISTANT_TRANSCRIPT_SOURCES,
    CANONICAL_ASSISTANT_TRANSCRIPT_SOURCE_V2,
    CANONICAL_IDENTIFIER_VALUES,
    LEGACY_ASSISTANT_TRANSCRIPT_SOURCE_V1,
    LEGACY_IDENTIFIER_VALUES,
)


class ConversationProvenanceIdentifierTests(unittest.TestCase):
    def test_legacy_registry_is_exact_and_frozen(self) -> None:
        self.assertIsInstance(LEGACY_IDENTIFIER_VALUES, frozenset)
        self.assertEqual(
            LEGACY_IDENTIFIER_VALUES,
            {
                "backend/resse:assistant:v1",
                "resse_response_v0_2",
                "resse_response_v0_3",
                "resse_response_v0_4",
                "resse_response_shadow_trace_v0_6",
                "resse_safety_assessor_v0_2",
                "resse_v0_2",
                "RESSE",
            },
        )

    def test_canonical_registry_is_exact_and_product_neutral(self) -> None:
        self.assertIsInstance(CANONICAL_IDENTIFIER_VALUES, frozenset)
        self.assertEqual(
            CANONICAL_IDENTIFIER_VALUES,
            {
                "backend/seebx:assistant:v2",
                "conversation_response_v1",
                "lifeswitch_response_v1",
                "conversation_response_shadow_trace_v1",
                "conversation_safety_assessor_v1",
                "conversation_response_v1",
                "default_response_policy",
            },
        )
        self.assertTrue(
            all("resse" not in value.casefold() for value in CANONICAL_IDENTIFIER_VALUES)
        )

    def test_new_writes_are_canonical_and_historical_sources_are_exact(self) -> None:
        self.assertEqual(
            ACCEPTED_ASSISTANT_TRANSCRIPT_SOURCES,
            {
                LEGACY_ASSISTANT_TRANSCRIPT_SOURCE_V1,
                CANONICAL_ASSISTANT_TRANSCRIPT_SOURCE_V2,
            },
        )

    def test_legacy_and_canonical_namespaces_are_disjoint(self) -> None:
        self.assertTrue(
            LEGACY_IDENTIFIER_VALUES.isdisjoint(CANONICAL_IDENTIFIER_VALUES)
        )

    def test_all_active_legacy_literals_are_centralized(self) -> None:
        root = Path(__file__).resolve().parents[1]
        registry = root / "seebx/contracts/conversation_provenance.py"
        for path in (root / "seebx").rglob("*.py"):
            if path == registry:
                continue
            source = path.read_text()
            for value in LEGACY_IDENTIFIER_VALUES:
                self.assertNotIn(
                    f'"{value}"',
                    source,
                    msg=f"legacy identifier is defined outside registry: {path}",
                )


if __name__ == "__main__":
    unittest.main()
