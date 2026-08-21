from __future__ import annotations

import unittest

from seebx.capabilities.conversation.prior_lifeswitch_provenance_contract import (
    PriorLifeSwitchPreparedContextV1,
)
from seebx.capabilities.conversation.lifeswitch_composition import (
    IntegratedTrustedLifeSwitchResponseExecutionV2,
    LifeSwitchResponseStage,
)
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR, ANSWER, NOW, new_plan
from tests.test_openai_chat_provider_v1 import FakeClient, provider_response


class CurrentProvider:
    def __init__(self, context) -> None:
        self.context = context

    def prepare(self, **_kwargs):
        return self.context


class PriorProvider:
    def prepare(self, **_kwargs):
        return PriorLifeSwitchPreparedContextV1.create(
            status="OFF", database_accessed=False
        )


class LifeSwitchConversationCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_integrated_execution_uses_provider_neutral_memory_name(self) -> None:
        fields = IntegratedTrustedLifeSwitchResponseExecutionV2.model_fields
        self.assertIn("memory_provenance", fields)
        self.assertNotIn("successor_memory_provenance", fields)

    async def test_text_and_voice_share_the_same_downstream_root(self) -> None:
        message = "What were my macros Monday?"
        prepared_plan = await new_plan(message)
        base = prepared_plan.base_response_plan
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=base.thread_id,
            current_request_id=base.policy_input.request_id,
            current_message=message,
        )
        root = LifeSwitchResponseStage(
            openai_client=FakeClient(provider_response(content="Bound answer.")),
            context_provider=CurrentProvider(prepared_plan.lifeswitch_context),
            prior_provenance_provider=PriorProvider(),
            clock=lambda: NOW,
            answer_id_factory=lambda: ANSWER,
        )
        result = await root.execute(
            base_response_plan=base,
            conversation_snapshot=snapshot,
        )
        self.assertEqual(result.trusted_plan.prior_lifeswitch_status, "OFF")
        self.assertIsNotNone(result.finalized.lifeswitch_provenance_receipt)
        self.assertGreaterEqual(
            result.stage_timings.prior_lifeswitch_provenance_selection_ms, 0
        )


if __name__ == "__main__":
    unittest.main()
