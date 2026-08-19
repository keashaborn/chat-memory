from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"


class TrainingMyExercisesQueryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = ROUTER.read_text(encoding="utf-8")
        match = re.search(
            r"async def list_my_exercises\(.*?(?=\n@router\.)",
            source,
            flags=re.DOTALL,
        )
        if match is None:
            raise AssertionError("list_my_exercises route not found")
        cls.route = match.group(0).lower()

    def test_query_uses_declared_my_exercise_alias(self) -> None:
        self.assertIn("from {schema}.my_exercise as me", self.route)
        self.assertIn("where me.owner_user_id=$1::uuid", self.route)
        self.assertNotIn("wt.owner_user_id", self.route)

    def test_active_filter_uses_declared_my_exercise_alias(self) -> None:
        self.assertIn(
            'where_active = "" if include_inactive else "and me.is_active=true"',
            self.route,
        )
        self.assertNotIn("wt.is_active", self.route)


if __name__ == "__main__":
    unittest.main()
