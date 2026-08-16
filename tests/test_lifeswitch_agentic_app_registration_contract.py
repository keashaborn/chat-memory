from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LifeSwitchObsoleteRuntimeRetirementContractTest(unittest.TestCase):
    def test_app_does_not_register_retired_routes(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(source)
        for retired in (
            "create_lifeswitch_plan_app_router",
            "lifeswitch_plan_router",
            "lifeswitch_people_router",
            'prefix="/lifeswitch/sage"',
            '@app.post("/admin/usage/summary")',
            '@app.get("/admin/usage/overview")',
            '@app.get("/admin/usage/users")',
            '@app.get("/admin/usage/users/{target_user_id}")',
        ):
            self.assertNotIn(retired, source)


if __name__ == "__main__":
    unittest.main()
