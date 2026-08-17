from __future__ import annotations

import unittest

from pydantic import ValidationError

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_prompt_integration_v2 import (
    ContextKindV3,
    PromptReferenceContextBlockV3,
)
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from rag_engine.response_lifeswitch_integration_v2 import (
    TrustedLifeSwitchResponsePlanV2,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import (
    TODAY,
    new_plan,
    off_prior,
)
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)
from tests.test_prompt_assembler_v1 import successor_memory_block


async def zep_plan(message: str = "What is my temporary recall phrase?"):
    base = await orchestrator(FixedSafetyProvider()).build_plan(
        trusted_request(
            authenticated_actor_user_id=ACTOR,
            request_id="request-123",
            conversation=messages(message),
            successor_memory_context_block=successor_memory_block(
                message,
                block_id="zep_memory_v1",
                source_contract_version="zep-cloud-context-v1",
            ),
        )
    )
    context = LifeSwitchPreparedContextV1.create(
        status="OFF",
        timezone_source="not_requested",
        database_accessed=False,
        data_plan=create_lifeswitch_data_plan_v1(message, today=TODAY),
    )
    return TrustedLifeSwitchResponsePlanV2.create(
        base_response_plan=base,
        lifeswitch_context=context,
        prior_lifeswitch_context=off_prior(),
    )


class LifeSwitchPromptIntegrationV2Tests(unittest.IsolatedAsyncioTestCase):
    async def base_plan(self, message: str):
        return await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="request-123",
                conversation=messages(message),
            )
        )

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

    async def test_zep_memory_survives_lifeswitch_augmentation(self) -> None:
        plan = await zep_plan()
        memory = tuple(
            block
            for block in plan.assembled_prompt.context_blocks
            if block.kind is ContextKindV3.MEMORY
        )
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0].block_id, "zep_memory_v1")

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

    async def test_selected_lifeswitch_status_reaches_system_prompt(self) -> None:
        plan = await new_plan("What were my protein totals Monday?")

        self.assertIn(
            "LifeSwitch was checked and relevant structured records are supplied",
            plan.assembled_prompt.system_prompt,
        )
        self.assertEqual(
            plan.assembled_prompt.manifest.lifeswitch_source_status.value,
            "SELECTED",
        )

    async def test_off_lifeswitch_status_is_turn_specific(self) -> None:
        message = "Explain the difference between two ideas."
        base = await self.base_plan(message)
        context = LifeSwitchPreparedContextV1.create(
            status="OFF",
            timezone_source="not_requested",
            database_accessed=False,
            data_plan=create_lifeswitch_data_plan_v1(message, today=TODAY),
        )

        plan = TrustedLifeSwitchResponsePlanV2.create(
            base_response_plan=base,
            lifeswitch_context=context,
            prior_lifeswitch_context=off_prior(),
        )

        self.assertIn(
            "LifeSwitch was not consulted for this response",
            plan.assembled_prompt.system_prompt,
        )
        self.assertFalse(
            plan.assembled_prompt.manifest.lifeswitch_database_accessed
        )

    async def test_explicit_rm_context_survives_lifeswitch_augmentation(self) -> None:
        message = "Explain Relational Monism in plain language."
        base = await self.base_plan(message)
        context = LifeSwitchPreparedContextV1.create(
            status="OFF",
            timezone_source="not_requested",
            database_accessed=False,
            data_plan=create_lifeswitch_data_plan_v1(message, today=TODAY),
        )

        plan = TrustedLifeSwitchResponsePlanV2.create(
            base_response_plan=base,
            lifeswitch_context=context,
            prior_lifeswitch_context=off_prior(),
        )

        block_ids = tuple(
            block.block_id for block in plan.assembled_prompt.context_blocks
        )
        self.assertIn("relational_monism_v0_4", block_ids)
        self.assertNotIn("fractal_monism_v0_2", block_ids)

    async def test_off_adds_no_prior_block(self) -> None:
        plan = await new_plan("What are my macros?")
        self.assertNotIn(
            "prior_lifeswitch_provenance_v1",
            tuple(block.block_id for block in plan.assembled_prompt.context_blocks),
        )
        self.assertEqual(plan.assembled_prompt.manifest.prior_lifeswitch_response_count, 0)


if __name__ == "__main__":
    unittest.main()
