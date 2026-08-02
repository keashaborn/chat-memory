from __future__ import annotations

import unittest

from tests.test_lifeswitch_answer_provenance_receipt_v1 import new_plan


class LifeSwitchPromptIntegrationV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_provenance_follows_current_lifeswitch_context(self) -> None:
        plan = await new_plan(
            "Did you access my LifeSwitch nutrition day for Monday?", with_prior=True
        )
        self.assertEqual(
            tuple(block.block_id for block in plan.assembled_prompt.context_blocks),
            ("lifeswitch_domain_context_v1", "prior_lifeswitch_provenance_v1"),
        )
        manifest = plan.assembled_prompt.manifest
        self.assertEqual(manifest.prior_lifeswitch_response_count, 1)
        self.assertGreater(manifest.prior_lifeswitch_estimated_tokens, 0)

    async def test_off_adds_no_prior_block(self) -> None:
        plan = await new_plan("What are my macros?")
        self.assertNotIn(
            "prior_lifeswitch_provenance_v1",
            tuple(block.block_id for block in plan.assembled_prompt.context_blocks),
        )
        self.assertEqual(plan.assembled_prompt.manifest.prior_lifeswitch_response_count, 0)


if __name__ == "__main__":
    unittest.main()
