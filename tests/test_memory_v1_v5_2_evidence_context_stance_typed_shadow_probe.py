from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "scripts"
    / "memory_v1_v5_2_evidence_context_stance_typed_shadow_probe.py"
)
SPEC = importlib.util.spec_from_file_location("stance_typed_shadow_probe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvidenceContextStanceTypedShadowProbeTest(unittest.TestCase):
    def test_contract_is_exactly_scoped(self) -> None:
        self.assertEqual(
            str(MODULE.TARGET_CLAIM_ID),
            "2759c735-f629-468c-bbb8-8947ccb5fdbc",
        )
        self.assertEqual(MODULE.TARGET_PREDICATE, "stance.reported")
        self.assertEqual(MODULE.TARGET_REVISION, 2)
        self.assertEqual(
            MODULE.QUERY,
            "What have I said about how Fractal Monism can help people?",
        )

    def test_intent_contract_is_owner_governed_stance_recall(self) -> None:
        intent = MODULE.classify_memory_intent(
            MODULE.QUERY,
            request_classification="GENERAL",
        )
        self.assertTrue(intent["routes"]["governed_claims"])
        self.assertEqual(intent["memory_intent"], "personal_recall")
        self.assertEqual(intent["domains"], ["stance_recall"])
        self.assertEqual(
            intent["claim_context"]["allowed_predicates"],
            ["stance.reported"],
        )
        self.assertTrue(intent["claim_context"]["explicit_recall"])


if __name__ == "__main__":
    unittest.main()
