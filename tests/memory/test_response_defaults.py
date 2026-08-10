from __future__ import annotations

import unittest
from uuid import UUID

from rag_engine.assistant_response_preferences_v1 import (
    default_assistant_response_preferences_v1,
    render_assistant_response_preferences_v1,
)
from rag_engine.governed_memory.response_defaults import (
    SUCCESSOR_RESPONSE_DEFAULTS,
    SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT,
)
from rag_engine.response_policy_v0_2 import ResponseMode


class SuccessorResponseDefaultsTests(unittest.TestCase):
    def test_clean_default_reads_no_stored_or_test_preferences(self) -> None:
        self.assertEqual(
            SUCCESSOR_RESPONSE_DEFAULTS.contract_version,
            SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT,
        )
        self.assertIsNone(
            SUCCESSOR_RESPONSE_DEFAULTS.response_policy_overlay
        )

    def test_clean_default_preserves_empty_default_prompt_behavior(self) -> None:
        owner = UUID("11111111-1111-4111-8111-111111111111")
        old_render = render_assistant_response_preferences_v1(
            default_assistant_response_preferences_v1(owner),
            ResponseMode.ORDINARY,
        )
        clean_render = render_assistant_response_preferences_v1(
            SUCCESSOR_RESPONSE_DEFAULTS.response_policy_overlay,
            ResponseMode.ORDINARY,
        )
        self.assertEqual(clean_render, old_render)


if __name__ == "__main__":
    unittest.main()
