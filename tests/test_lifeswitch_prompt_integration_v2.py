from __future__ import annotations

import unittest

from pydantic import ValidationError

from rag_engine.lifeswitch_prompt_integration_v2 import (
    ContextKindV3,
    PromptReferenceContextBlockV3,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import new_plan


class LifeSwitchPromptIntegrationV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_memory_block_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            PromptReferenceContextBlockV3(
                block_id="governed_memory_v1",
                kind=ContextKindV3.MEMORY,
                source_contract_version="retired",
                source_manifest_sha256="a" * 64,
                request_id_sha256="b" * 64,
                query_sha256="c" * 64,
                content="retired",
                content_sha256="d" * 64,
                content_bytes=7,
                estimated_tokens=2,
                block_manifest_sha256="e" * 64,
            )

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
