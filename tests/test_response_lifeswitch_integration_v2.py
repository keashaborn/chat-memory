from __future__ import annotations

import unittest

from tests.test_lifeswitch_answer_provenance_receipt_v1 import new_plan


class ResponseLifeSwitchIntegrationV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_keeps_current_and_prior_sources_independent(self) -> None:
        plan = await new_plan(
            "Did you access my LifeSwitch nutrition day for Monday?", with_prior=True
        )
        self.assertEqual(plan.prior_lifeswitch_status, "SELECTED")
        self.assertTrue(plan.prior_lifeswitch_database_accessed)
        self.assertIsNotNone(plan.prior_lifeswitch_provenance)
        self.assertNotEqual(
            plan.lifeswitch_context_manifest_sha256,
            plan.prior_lifeswitch_provenance.manifest_sha256,
        )


if __name__ == "__main__":
    unittest.main()
