from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = (
    SCRIPTS
    / "memory_v1_v5_2_evidence_context_stance_controlled_project.py"
)
SPEC = importlib.util.spec_from_file_location(
    "evidence_context_stance_controlled_project",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvidenceContextStanceControlledProjectTest(unittest.TestCase):
    def test_exact_stance_shadow_query_is_topic_matched(self) -> None:
        self.assertEqual(
            MODULE.base.QUERY_BY_PREDICATE["stance.reported"],
            "What have I said about how Fractal Monism can help people?",
        )

    def test_other_projection_queries_remain_available(self) -> None:
        self.assertEqual(
            MODULE.base.QUERY_BY_PREDICATE["identity.name_canonical"],
            "Was it Nemo or Neko?",
        )


if __name__ == "__main__":
    unittest.main()
