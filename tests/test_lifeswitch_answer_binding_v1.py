from __future__ import annotations

import datetime as dt
import unittest
import uuid

from seebx.capabilities.conversation.lifeswitch_answer_binding import (
    FinalAnswerLifeSwitchBindingV1,
)
from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
)
from seebx.capabilities.conversation.lifeswitch_plan import (
    TrustedLifeSwitchResponsePlanV2,
)
from tests.test_lifeswitch_answer_provenance_receipt_v1 import new_plan, off_prior
from tests.test_response_orchestration_v0_2 import (
    ACTOR,
    FixedSafetyProvider,
    messages,
    orchestrator,
    trusted_request,
)


class LifeSwitchAnswerBindingV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_context_creates_separate_answer_binding(self) -> None:
        message = "Was I low on protein Monday?"
        assembly = (await new_plan(message)).assembled_prompt

        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=assembly,
            authenticated_actor_user_id=ACTOR,
            answer_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            created_at=dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc),
        )

        self.assertIsNotNone(binding)
        self.assertTrue(binding.answer_model_exposed)
        self.assertEqual(binding.record_count, 1)
        self.assertEqual(binding.record_refs[0].projection, "nutrition_day")
        self.assertNotIn("daily", binding.model_dump_json())

    async def test_no_context_creates_no_lifeswitch_binding(self) -> None:
        message = "Hello there."
        base = await orchestrator(FixedSafetyProvider()).build_plan(
            trusted_request(
                authenticated_actor_user_id=ACTOR,
                request_id="no-lifeswitch-binding",
                conversation=messages(message),
            )
        )
        data_plan = create_lifeswitch_data_plan_v1(
            message,
            today=dt.date(2026, 7, 29),
        )
        self.assertFalse(data_plan.data_access)
        context = LifeSwitchPreparedContextV1.create(
            status="OFF",
            timezone_source="not_requested",
            database_accessed=False,
            data_plan=data_plan,
        )
        plan = TrustedLifeSwitchResponsePlanV2.create(
            base_response_plan=base,
            lifeswitch_context=context,
            prior_lifeswitch_context=off_prior(),
        )

        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=plan.assembled_prompt,
            authenticated_actor_user_id=ACTOR,
            answer_id=uuid.uuid4(),
            created_at=dt.datetime.now(dt.timezone.utc),
        )

        self.assertIsNone(binding)

    async def test_cross_owner_binding_is_rejected(self) -> None:
        message = "Was I low on protein Monday?"
        assembly = (await new_plan(message)).assembled_prompt

        with self.assertRaisesRegex(ValueError, "actor differs"):
            FinalAnswerLifeSwitchBindingV1.create(
                assembly=assembly,
                authenticated_actor_user_id=uuid.uuid4(),
                answer_id=uuid.uuid4(),
                created_at=dt.datetime.now(dt.timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
