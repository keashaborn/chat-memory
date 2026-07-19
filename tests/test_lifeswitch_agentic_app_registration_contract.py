from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LifeSwitchPlanAppRegistrationContractTest(unittest.TestCase):
    def test_app_registers_agentic_plan_router_under_existing_plan_prefix(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn(
            "from lifeswitch_agentic.app_adapter import create_lifeswitch_plan_app_router",
            source,
        )
        self.assertIn("create_lifeswitch_plan_app_router(", source)
        self.assertIn('prefix="/lifeswitch/plan"', source)
        self.assertIn('openai_api_key=os.getenv("OPENAI_API_KEY")', source)
        self.assertIn('os.getenv("LIFESWITCH_PLAN_MODEL")', source)


if __name__ == "__main__":
    unittest.main()
