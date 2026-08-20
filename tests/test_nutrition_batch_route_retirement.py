from __future__ import annotations

import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODULE = ROOT / "seebx" / "capabilities" / "nutrition" / "batch.py"
CANONICAL_MODULE = ROOT / "seebx" / "capabilities" / "nutrition" / "logs.py"


class NutritionBatchRouteRetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(
            os.environ,
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                )
            },
            clear=False,
        ):
            cls.backend = importlib.import_module("app")

    def test_legacy_nontransactional_batch_module_is_absent(self) -> None:
        self.assertFalse(LEGACY_MODULE.exists())
        composition = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("seebx.capabilities.nutrition.batch", composition)
        self.assertNotIn("lifeswitch_nutrition_log_batch_router", composition)

    def test_canonical_atomic_batch_route_remains(self) -> None:
        source = CANONICAL_MODULE.read_text(encoding="utf-8")
        self.assertIn('@router.post("/log/entries/batch")', source)
        self.assertIn("async with conn.transaction():", source)
        self.assertIn("and owner_user_id=$2::uuid", source)
        self.assertIn("max_length=100", source)

        paths = self.backend.app.openapi()["paths"]
        self.assertNotIn("/lifeswitch/nutrition/log/entries", paths)
        self.assertIn("/lifeswitch/nutrition/log/entries/batch", paths)
        self.assertIn(
            "post",
            paths["/lifeswitch/nutrition/log/entries/batch"],
        )


if __name__ == "__main__":
    unittest.main()
