from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = (
    SCRIPTS / "memory_v1_v5_2_self_identity_name_controlled_project.py"
)
SPEC = importlib.util.spec_from_file_location(
    "self_identity_name_controlled_project",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SelfIdentityNameControlledProjectTest(unittest.TestCase):
    def test_self_name_shadow_query_is_not_the_pet_name_query(self) -> None:
        self.assertEqual(
            MODULE.base.QUERY_BY_PREDICATE["identity.name"],
            "What is my name?",
        )

    def test_canonical_pet_name_query_remains_available(self) -> None:
        self.assertEqual(
            MODULE.base.QUERY_BY_PREDICATE["identity.name_canonical"],
            "Was it Nemo or Neko?",
        )


if __name__ == "__main__":
    unittest.main()
