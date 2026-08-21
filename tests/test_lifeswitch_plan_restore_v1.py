from pathlib import Path
import asyncio
import unittest

from fastapi import HTTPException

from seebx.capabilities.plans.routes import _resolve_plan_target


ROOT = Path(__file__).resolve().parents[1]


class LifeSwitchPlanRestoreTests(unittest.TestCase):
    def test_target_resolution_is_owner_only(self):
        owner = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(
            asyncio.run(_resolve_plan_target(owner)),
            (owner, False),
        )
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(_resolve_plan_target(owner, other))
        self.assertEqual(raised.exception.status_code, 403)

    def test_router_uses_isolated_owner_bound_connection(self):
        source = (ROOT / "seebx" / "capabilities" / "plans" / "routes.py").read_text()
        self.assertIn("from seebx.adapters.lifeswitch_plan_postgres import (", source)
        self.assertIn("lifeswitch_plan_repository", source)
        self.assertNotIn("connect_lifeswitch", source)
        self.assertNotIn('os.getenv("POSTGRES_DSN")', source)
        self.assertNotIn("lifeswitch_people", source)
        self.assertIn('detail="owner-only Plan access required"', source)

    def test_app_registers_only_canonical_owner_plan_router(self):
        source = (ROOT / "app.py").read_text()
        self.assertIn("from seebx.capabilities.plans.routes import", source)
        self.assertIn(
            'app.include_router(lifeswitch_plan_router, prefix="/lifeswitch/plan")',
            source,
        )
        self.assertNotIn("create_lifeswitch_plan_app_router", source)

    def test_plan_migration_is_empty_and_forced_rls(self):
        source = (
            ROOT / "ops" / "sql" / "20260816_lifeswitch_plan_structure_v1.sql"
        ).read_text()
        self.assertIn("SET ROLE lifeswitch_owner", source)
        self.assertIn("FORCE ROW LEVEL SECURITY", source)
        self.assertIn("TO lifeswitch_app", source)
        self.assertNotIn("INSERT INTO", source.upper())
        self.assertNotIn("lifeswitch_people", source)


if __name__ == "__main__":
    unittest.main()
